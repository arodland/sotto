# K6 voice pager, receive side (see README.md).
#   make -C c/k6            pager_host: the pager's packet and playback path on this machine,
#                           with the Cortex-M0 decoder path (FX_NARROW_MUL), for the tests
#   sh tools/k6_build.sh    libsotto_k6.a for the radio (thumbv6m, clang)
#   km_host                 the module loader (k6_mod.c) with the test key 00..0f
#   pager_host_lr           the same with -DSP_LR: plays sotto 1200 (magic 0xA9) messages
# Always the v3.7 8 kHz core, whatever VARIANT the rest of c/ is built with: the packet
# format's magic 0xA7 means v3.7 frames, 0xA9 sotto 1200 (on the v3.7 core).
# SPDX-License-Identifier: MIT
CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
K6FLAGS = -std=c99 -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DFX_NARROW_MUL -DFX_CHECK -I.. -DFX_CS512_CONST
DECFLAGS = -Dcalloc=sp_calloc -Dfree=sp_free
OBJS = host/sotto_pager.o host/sotto_fx_dec.o host/fx_math.o host/fx_tables.o

LR_OBJS = host-lr/sotto_pager.o host-lr/sotto_fx_dec.o host-lr/sotto_fx_lr.o host/fx_math.o host/fx_tables.o

all: pager_host pager_host_lr km_host
km_host: host/km_host.o host/k6_mod_test.o $(OBJS)
	$(CC) -o $@ $^
host/k6_mod_test.o: k6_mod.c k6_mod.h sotto_pager.h
	@mkdir -p host
	$(CC) $(CFLAGS) $(K6FLAGS) -DKM_KEY=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15 -c -o $@ $<
pager_host: host/pager_host.o $(OBJS)
	$(CC) -o $@ $^
pager_host_lr: host-lr/pager_host.o $(LR_OBJS)
	$(CC) -o $@ $^
host-lr/%.o: %.c sotto_pager.h
	@mkdir -p host-lr
	$(CC) $(CFLAGS) $(K6FLAGS) -DSP_LR -c -o $@ $<
host-lr/sotto_fx_dec.o: ../fixed/sotto_fx_dec.c ../fixed/fx.h ../fixed/fx_internal.h ../fixed/fx_tables.h
	@mkdir -p host-lr
	$(CC) $(CFLAGS) $(K6FLAGS) $(DECFLAGS) -DSP_LR -c -o $@ $<
host-lr/sotto_fx_lr.o: ../fixed/sotto_fx_lr.c ../sotto_lr_dec.inc ../sotto_lr_tables.h ../fixed/fx.h ../fixed/fx_internal.h
	@mkdir -p host-lr
	$(CC) $(CFLAGS) $(K6FLAGS) $(DECFLAGS) -DSP_LR -c -o $@ $<
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
	rm -rf host host-lr pager_host pager_host_lr km_host build build-lr
.PHONY: all clean
