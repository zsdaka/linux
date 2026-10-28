CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra
LDFLAGS ?= -lm

.PHONY: all run

all: hws_sim

hws_sim: hws_sim.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

run: hws_sim
	mkdir -p out
	./hws_sim out
