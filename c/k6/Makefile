# K6 voice pager, receive side (see README.md).
#   make -C c/k6            pager_host: the pager's packet and playback path on this machine,
#                           with the Cortex-M0 decoder path (FX_NARROW_MUL), for the tests
#   sh tools/k6_build.sh    libsotto_k6.a for the radio (thumbv6m, clang)
# Always the v3.7 8 kHz decoder, whatever VARIANT the rest of c/ is built with: the packet
# format's magic 0xA7 means v3.7 frames.  SPDX-License-Identifier: MIT
CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
K6FLAGS = -std=c99 -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DFX_NARROW_MUL -DFX_CHECK -I.. -DFX_CS512_CONST
DECFLAGS = -Dcalloc=sp_calloc -Dfree=sp_free
OBJS = host/sotto_pager.o host/sotto_fx_dec.o host/fx_math.o host/fx_tables.o

pager_host: host/pager_host.o $(OBJS)
	$(CC) -o $@ $^
host/%.o: %.c sotto_pager.h
	@mkdir -p host
	$(CC) $(CFLAGS) $(K6FLAGS) -c -o $@ $<
host/sotto_fx_dec.o: ../fixed/sotto_fx_dec.c ../fixed/fx.h ../fixed/fx_internal.h ../fixed/fx_tables.h
	@mkdir -p host
	$(CC) $(CFLAGS) $(K6FLAGS) $(DECFLAGS) -c -o $@ $<
host/%.o: ../fixed/%.c ../fixed/fx.h ../fixed/fx_tables.h
	@mkdir -p host
	$(CC) $(CFLAGS) $(K6FLAGS) -c -o $@ $<
clean:
	rm -rf host pager_host build
.PHONY: clean
