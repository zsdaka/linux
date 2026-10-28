/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Userspace model of the mlx5 HWS matcher geometry in this tree:
 *   main RTC:  2^row_log rows, depth 4  (log_depth = 2)
 *   collision: 2^(row_log-5) rows, depth 16 (log_depth = 4)
 * Insert cost is 1 GTA WQE on the main RTC, plus 1 retry WQE when the
 * main row is full. This is not a NIC benchmark.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DEPTH_MAIN 4
#define DEPTH_COL 16
#define ROW_RATIO 5
#define HIST_MAX 48
#define ZIPF_S 1.2
#define ZIPF_U 1048576u /* 2^20 source groups */

enum data_kind {
	DATA_UNIFORM = 0,
	DATA_UNIFORM_ALT = 1,
	DATA_ZIPF_COARSE = 2,
	DATA_ZIPF_MIXED = 3,
	DATA_TRACE = 4,
};

struct run_cfg {
	const char *exp;
	const char *data_name;
	enum data_kind kind;
	uint64_t n;
	int row_log;
	int use_col;
	const char *trace_path;
	const struct trace_keys *keys;
};

struct run_out {
	uint64_t n;
	uint64_t step1;
	uint64_t step2;
	uint64_t fail;
	uint64_t overflow;
	uint64_t main_occ;
	uint64_t col_occ;
	uint64_t lookup_ok;
	uint64_t lookup_miss_ok;
	uint64_t lookup_bad;
	uint32_t lookup_max_cmp;
	uint64_t lookup_cmp_sum;
	uint64_t hist[HIST_MAX];
	double seconds;
	double theory_over;
	double theory_fail;
	int depth_main;
	int depth_col;
	uint64_t rows;
	uint64_t col_rows;
};

static double now_sec(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static uint64_t mix64(uint64_t x)
{
	x ^= x >> 30;
	x *= 0xbf58476d1ce4e5b9ULL;
	x ^= x >> 27;
	x *= 0x94d049bb133111ebULL;
	x ^= x >> 31;
	return x;
}

static uint64_t mix64_alt(uint64_t x)
{
	x ^= x >> 33;
	x *= 0xff51afd7ed558ccdULL;
	x ^= x >> 33;
	x *= 0xc4ceb9fe1a85ec53ULL;
	x ^= x >> 33;
	return x;
}

static double poisson_excess(double lambda, int depth)
{
	double p, excess;
	int k;

	if (lambda <= 0.0)
		return 0.0;
	p = exp(-lambda);
	excess = 0.0;
	for (k = 0; k < 200; k++) {
		if (k > depth)
			excess += (double)(k - depth) * p;
		p *= lambda / (double)(k + 1);
		if (p < 1e-20 && k > lambda + 20)
			break;
	}
	return excess;
}

static double theory_overflow_frac(double lambda, int depth)
{
	if (lambda <= 0.0)
		return 0.0;
	return poisson_excess(lambda, depth) / lambda;
}

static double theory_fail_frac(uint64_t n, uint64_t rows, uint64_t col_rows,
			       int depth, int cdepth)
{
	double lambda, f, n_over, lambda_c;

	if (!n || !rows)
		return 0.0;
	lambda = (double)n / (double)rows;
	f = theory_overflow_frac(lambda, depth);
	if (!col_rows || f <= 0.0)
		return f;
	n_over = (double)n * f;
	lambda_c = n_over / (double)col_rows;
	return f * theory_overflow_frac(lambda_c, cdepth);
}

struct zipf {
	double *cdf;
	uint32_t u;
	uint64_t rng;
};

static int zipf_init(struct zipf *z)
{
	double h = 0.0, acc = 0.0;
	uint32_t k;

	z->u = ZIPF_U;
	z->rng = 0x123456789abcdefULL;
	z->cdf = malloc(sizeof(double) * z->u);
	if (!z->cdf)
		return -1;
	for (k = 1; k <= z->u; k++)
		h += pow((double)k, -ZIPF_S);
	for (k = 1; k <= z->u; k++) {
		acc += pow((double)k, -ZIPF_S) / h;
		z->cdf[k - 1] = acc;
	}
	z->cdf[z->u - 1] = 1.0;
	return 0;
}

static uint32_t zipf_sample(struct zipf *z)
{
	uint64_t r = mix64(z->rng += 0x9e3779b97f4a7c15ULL);
	double u = (r >> 11) * (1.0 / 9007199254740992.0);
	uint32_t lo = 0, hi = z->u;

	while (lo + 1 < hi) {
		uint32_t mid = lo + (hi - lo) / 2;

		if (z->cdf[mid] < u)
			lo = mid;
		else
			hi = mid;
	}
	return lo + 1;
}

struct trace_keys {
	uint64_t *key;
	uint64_t n;
};

static int trace_load(const char *path, struct trace_keys *t)
{
	FILE *f;
	uint64_t n = 0, cap = 0, v;

	f = fopen(path, "rb");
	if (!f)
		return -1;
	if (fread(&n, sizeof(n), 1, f) != 1) {
		fclose(f);
		return -1;
	}
	t->key = malloc(sizeof(uint64_t) * n);
	if (!t->key) {
		fclose(f);
		return -1;
	}
	cap = fread(t->key, sizeof(uint64_t), n, f);
	fclose(f);
	if (cap != n) {
		free(t->key);
		return -1;
	}
	t->n = n;
	(void)v;
	return 0;
}

static void place_row(uint64_t *slot, uint32_t *arrival, uint64_t row,
		      int depth, uint64_t fp, int *placed)
{
	uint64_t base = row * (uint64_t)depth;
	int i;

	arrival[row]++;
	if (arrival[row] <= (uint32_t)depth) {
		for (i = 0; i < depth; i++) {
			if (slot[base + i] == 0) {
				slot[base + i] = fp;
				*placed = 1;
				return;
			}
		}
	}
	*placed = 0;
}

static int lookup_row(const uint64_t *slot, uint64_t row, int depth,
		      uint64_t fp, uint32_t *cmps)
{
	uint64_t base = row * (uint64_t)depth;
	int i;

	for (i = 0; i < depth; i++) {
		(*cmps)++;
		if (slot[base + i] == 0)
			return 0;
		if (slot[base + i] == fp)
			return 1;
	}
	return 0;
}

static int run_one(const struct run_cfg *cfg, struct zipf *zip,
		   struct run_out *o)
{
	uint64_t rows = 1ULL << cfg->row_log;
	uint64_t col_rows = 0;
	int depth = DEPTH_MAIN;
	int cdepth = DEPTH_COL;
	uint64_t *main_slot = NULL, *col_slot = NULL;
	uint32_t *arrival = NULL;
	const struct trace_keys *trace = cfg->keys;
	uint64_t i, fp, rng0 = 0;
	double t0;
	int rc = 0;

	if (zip)
		rng0 = zip->rng;

	memset(o, 0, sizeof(*o));
	o->rows = rows;
	o->depth_main = depth;
	o->depth_col = cfg->use_col ? cdepth : 0;
	if (cfg->use_col) {
		if (cfg->row_log <= ROW_RATIO)
			col_rows = 1;
		else
			col_rows = 1ULL << (cfg->row_log - ROW_RATIO);
	}
	o->col_rows = col_rows;
	o->n = cfg->n;
	o->theory_over = theory_overflow_frac((double)cfg->n / (double)rows, depth);
	o->theory_fail = theory_fail_frac(cfg->n, rows, col_rows, depth, cdepth);

	main_slot = calloc(rows * (uint64_t)depth, sizeof(uint64_t));
	arrival = calloc(rows, sizeof(uint32_t));
	if (col_rows)
		col_slot = calloc(col_rows * (uint64_t)cdepth, sizeof(uint64_t));
	if (!main_slot || !arrival || (col_rows && !col_slot)) {
		rc = -ENOMEM;
		goto out;
	}

	t0 = now_sec();
	for (i = 0; i < cfg->n; i++) {
		uint64_t h, h2, row, crow = 0;
		int placed = 0;

		fp = i + 1;
		switch (cfg->kind) {
		case DATA_UNIFORM:
			h = mix64(fp);
			h2 = mix64_alt(fp);
			break;
		case DATA_UNIFORM_ALT:
			h = mix64_alt(fp);
			h2 = mix64(fp ^ 0x9e3779b97f4a7c15ULL);
			break;
		case DATA_ZIPF_COARSE: {
			uint32_t g = zipf_sample(zip);

			h = mix64((uint64_t)g * 0x100000001ULL);
			h2 = mix64_alt(fp);
			break;
		}
		case DATA_ZIPF_MIXED: {
			uint32_t g = zipf_sample(zip);

			h = mix64(((uint64_t)g << 32) ^ fp);
			h2 = mix64_alt(fp ^ ((uint64_t)g << 17));
			break;
		}
		case DATA_TRACE: {
			uint64_t key = trace->key[i % trace->n];

			h = mix64(key);
			h2 = mix64_alt(key ^ fp);
			break;
		}
		default:
			h = mix64(fp);
			h2 = mix64_alt(fp);
			break;
		}
		row = h & (rows - 1);
		place_row(main_slot, arrival, row, depth, fp, &placed);
		if (placed) {
			o->step1++;
			o->main_occ++;
			continue;
		}
		o->overflow++;
		if (!col_rows) {
			o->fail++;
			continue;
		}
		crow = h2 & (col_rows - 1);
		{
			uint64_t base = crow * (uint64_t)cdepth;
			int s;

			for (s = 0; s < cdepth; s++) {
				if (col_slot[base + s] == 0) {
					col_slot[base + s] = fp;
					placed = 1;
					break;
				}
			}
		}
		if (placed) {
			o->step2++;
			o->col_occ++;
		} else {
			o->fail++;
		}
		if ((i & 0x3fffff) == 0 && i)
			fprintf(stderr, "  %s/%s %" PRIu64 "\n", cfg->exp,
				cfg->data_name, i);
	}

	for (i = 0; i < rows; i++) {
		uint32_t c = arrival[i];

		if (c >= HIST_MAX)
			c = HIST_MAX - 1;
		o->hist[c]++;
	}

	/* Lookup every inserted key. Failed inserts must miss. */
	{
		struct zipf zlook;

		if (zip) {
			zlook = *zip;
			zlook.rng = rng0;
		} else {
			memset(&zlook, 0, sizeof(zlook));
		}
		for (i = 0; i < cfg->n; i++) {
			uint64_t h, h2, row;
			uint32_t cmps = 0;
			int found = 0;
			int expect = 1;

			fp = i + 1;
			switch (cfg->kind) {
			case DATA_UNIFORM:
				h = mix64(fp);
				h2 = mix64_alt(fp);
				break;
			case DATA_UNIFORM_ALT:
				h = mix64_alt(fp);
				h2 = mix64(fp ^ 0x9e3779b97f4a7c15ULL);
				break;
			case DATA_ZIPF_COARSE: {
				uint32_t g = zipf_sample(&zlook);

				h = mix64((uint64_t)g * 0x100000001ULL);
				h2 = mix64_alt(fp);
				break;
			}
			case DATA_ZIPF_MIXED: {
				uint32_t g = zipf_sample(&zlook);

				h = mix64(((uint64_t)g << 32) ^ fp);
				h2 = mix64_alt(fp ^ ((uint64_t)g << 17));
				break;
			}
			case DATA_TRACE: {
				uint64_t key = trace->key[i % trace->n];

				h = mix64(key);
				h2 = mix64_alt(key ^ fp);
				break;
			}
			default:
				h = mix64(fp);
				h2 = mix64_alt(fp);
				break;
			}
			row = h & (rows - 1);
			found = lookup_row(main_slot, row, depth, fp, &cmps);
			if (!found && col_rows) {
				uint64_t crow = h2 & (col_rows - 1);

				found = lookup_row(col_slot, crow, cdepth, fp, &cmps);
			}
			/*
			 * expect is recovered from occupancy: a key that did not
			 * fit is absent. We detect that by a second pass only
			 * through found/not. The insert loop did not record
			 * per-key status, so compare against a replay flag
			 * stored implicitly: fingerprint present iff success.
			 * Count mismatches after classifying by presence.
			 */
			(void)expect;
			if (found)
				o->lookup_ok++;
			else
				o->lookup_miss_ok++;
			if (cmps > o->lookup_max_cmp)
				o->lookup_max_cmp = cmps;
			o->lookup_cmp_sum += cmps;
		}
		if (o->lookup_ok + o->lookup_miss_ok != cfg->n)
			o->lookup_bad = 1;
		if (o->lookup_ok != o->step1 + o->step2)
			o->lookup_bad++;
		if (o->lookup_miss_ok != o->fail)
			o->lookup_bad++;
		if (o->lookup_max_cmp > (uint32_t)(depth + (col_rows ? cdepth : 0)))
			o->lookup_bad++;
	}

	o->seconds = now_sec() - t0;
	fprintf(stderr,
		"done %s/%s n=%" PRIu64 " step1=%" PRIu64 " step2=%" PRIu64
		" fail=%" PRIu64 " over=%" PRIu64 " look_bad=%" PRIu64
		" t=%.2fs\n",
		cfg->exp, cfg->data_name, o->n, o->step1, o->step2, o->fail,
		o->overflow, o->lookup_bad, o->seconds);

out:
	free(main_slot);
	free(col_slot);
	free(arrival);
	return rc;
}

static void write_summary(FILE *f, const struct run_cfg *cfg, const struct run_out *o)
{
	double u_main, u_col, over_frac, fail_frac;
	uint64_t main_slots = o->rows * (uint64_t)o->depth_main;
	uint64_t col_slots = o->col_rows * (uint64_t)(o->col_rows ? DEPTH_COL : 0);

	u_main = main_slots ? (double)o->main_occ / (double)main_slots : 0.0;
	u_col = col_slots ? (double)o->col_occ / (double)col_slots : 0.0;
	over_frac = o->n ? (double)o->overflow / (double)o->n : 0.0;
	fail_frac = o->n ? (double)o->fail / (double)o->n : 0.0;
	fprintf(f,
		"%s,%s,%" PRIu64 ",%" PRIu64 ",%d,%" PRIu64 ",%d,%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%.8f,%.8f,%.8f,%.8f,%.8f,%.8f,%u,%" PRIu64
		",%" PRIu64 ",%.3f\n",
		cfg->exp, cfg->data_name, o->n, o->rows, o->depth_main, o->col_rows,
		o->depth_col, o->step1, o->step2, o->fail, o->overflow, over_frac,
		o->theory_over, fail_frac, o->theory_fail, u_main, u_col,
		o->lookup_max_cmp, o->lookup_bad, o->lookup_cmp_sum, o->seconds);
}

static void write_hist(FILE *f, const struct run_cfg *cfg, const struct run_out *o)
{
	double lambda = (double)o->n / (double)o->rows;
	double p = exp(-lambda);
	int k;

	for (k = 0; k < HIST_MAX; k++) {
		double expect = (double)o->rows * p;

		fprintf(f, "%s,%s,%d,%" PRIu64 ",%.2f\n", cfg->exp, cfg->data_name,
			k, o->hist[k], expect);
		p *= lambda / (double)(k + 1);
	}
}

static int emit(const struct run_cfg *cfg, struct zipf *zip, FILE *sum, FILE *hist)
{
	struct run_out o;
	int rc = run_one(cfg, zip, &o);

	if (rc)
		return rc;
	write_summary(sum, cfg, &o);
	fflush(sum);
	write_hist(hist, cfg, &o);
	fflush(hist);
	if (o.step1 + o.step2 + o.fail != o.n) {
		fprintf(stderr, "accounting mismatch\n");
		return -1;
	}
	if (o.lookup_bad) {
		fprintf(stderr, "lookup check failed\n");
		return -1;
	}
	/* Successful inserts take 1 or 2 WQEs. A 3rd step cannot occur. */
	if (o.lookup_max_cmp > (uint32_t)(DEPTH_MAIN + (o.col_rows ? DEPTH_COL : 0)))
		return -1;
	return 0;
}

int main(int argc, char **argv)
{
	const char *out_dir = "out";
	const char *trace_path = NULL;
	const char *resample_path = NULL;
	char sum_path[512], hist_path[512];
	FILE *sum, *hist;
	struct zipf zip;
	struct trace_keys trace, resample;
	struct run_cfg cfgs[16];
	int ncfg = 0;
	int i, rc;
	int trace_only = 0;

	memset(&trace, 0, sizeof(trace));
	memset(&resample, 0, sizeof(resample));
	if (argc >= 2)
		out_dir = argv[1];
	if (argc >= 3 && argv[2][0])
		trace_path = argv[2];
	if (argc >= 4 && argv[3][0])
		resample_path = argv[3];
	if (argc >= 5 && strcmp(argv[4], "trace-only") == 0)
		trace_only = 1;

	if (zipf_init(&zip)) {
		fprintf(stderr, "zipf init failed\n");
		return 1;
	}
	if (trace_path && trace_path[0]) {
		if (trace_load(trace_path, &trace)) {
			fprintf(stderr, "trace load failed: %s\n", trace_path);
			return 1;
		}
		fprintf(stderr, "trace keys %" PRIu64 "\n", trace.n);
	}
	if (resample_path && resample_path[0]) {
		if (trace_load(resample_path, &resample)) {
			fprintf(stderr, "resample load failed: %s\n", resample_path);
			return 1;
		}
		fprintf(stderr, "resample keys %" PRIu64 "\n", resample.n);
	}

	snprintf(sum_path, sizeof(sum_path), "%s/summary.csv", out_dir);
	snprintf(hist_path, sizeof(hist_path), "%s/hist.csv", out_dir);
	sum = fopen(sum_path, trace_only ? "a" : "w");
	hist = fopen(hist_path, trace_only ? "a" : "w");
	if (!sum || !hist) {
		perror("fopen");
		return 1;
	}
	if (!trace_only) {
		fprintf(sum,
			"exp,data,n,rows,depth,col_rows,col_depth,step1,step2,fail,overflow,"
			"over_frac,theory_over,fail_frac,theory_fail,u_main,u_col,"
			"lookup_max_cmp,lookup_bad,lookup_cmp_sum,seconds\n");
		fprintf(hist, "exp,data,k,count,poisson_expect\n");
	}

#define ADD(e, dname, knd, nn, rlog, col)                                      \
	do {                                                                    \
		cfgs[ncfg].exp = (e);                                           \
		cfgs[ncfg].data_name = (dname);                                 \
		cfgs[ncfg].kind = (knd);                                        \
		cfgs[ncfg].n = (nn);                                            \
		cfgs[ncfg].row_log = (rlog);                                    \
		cfgs[ncfg].use_col = (col);                                     \
		cfgs[ncfg].trace_path = NULL;                                   \
		cfgs[ncfg].keys = NULL;                                         \
		ncfg++;                                                         \
	} while (0)

	if (!trace_only) {
	ADD("A", "uniform", DATA_UNIFORM, 10000000ULL, 24, 1);
	ADD("B", "uniform", DATA_UNIFORM, 16777216ULL, 24, 1);
	ADD("C", "uniform", DATA_UNIFORM, 20000000ULL, 24, 1);
	ADD("A", "uniform_alt", DATA_UNIFORM_ALT, 10000000ULL, 24, 1);
	ADD("A", "zipf_coarse", DATA_ZIPF_COARSE, 10000000ULL, 24, 1);
	ADD("B", "zipf_coarse", DATA_ZIPF_COARSE, 16777216ULL, 24, 1);
	ADD("C", "zipf_coarse", DATA_ZIPF_COARSE, 20000000ULL, 24, 1);
	ADD("A", "zipf_mixed", DATA_ZIPF_MIXED, 10000000ULL, 24, 1);
	ADD("Dtight", "uniform", DATA_UNIFORM, 16777216ULL, 22, 1);
	ADD("Ablation", "uniform", DATA_UNIFORM, 10000000ULL, 24, 0);
	}
	if (trace.n) {
		ADD("TraceFull", "ctu", DATA_TRACE, trace.n, 24, 1);
		cfgs[ncfg - 1].keys = &trace;
	}
	if (resample.n >= 10000000ULL) {
		ADD("A", "ctu_resample", DATA_TRACE, 10000000ULL, 24, 1);
		cfgs[ncfg - 1].keys = &resample;
	}
#undef ADD

	for (i = 0; i < ncfg; i++) {
		fprintf(stderr, "run %s %s n=%" PRIu64 " row_log=%d col=%d\n",
			cfgs[i].exp, cfgs[i].data_name, cfgs[i].n,
			cfgs[i].row_log, cfgs[i].use_col);
		rc = emit(&cfgs[i], &zip, sum, hist);
		if (rc) {
			fprintf(stderr, "run failed %d\n", rc);
			return 1;
		}
	}
	fclose(sum);
	fclose(hist);
	free(zip.cdf);
	free(trace.key);
	free(resample.key);
	return 0;
}
