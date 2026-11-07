# K6 voice pager, receive side (see README.md).
#   make -C c/k6            pager_host: the pager's packet and playback path on this machine,
#                           with the Cortex-M0 decoder path (FX_NARROW_MUL), for the tests
#   sh tools/k6_build.sh    libsotto_k6.a for the radio (thumbv6m, clang)
#   km_host                 the module loader (k6_mod.c) with the test key 00..0f
#   pager_host_lr           the same with -DSP_LR: plays sotto 1200 (magic 0xA9) messages
#   pager_host_cbs, pager_host_lr_cbs   the same two with the codebook in (emulated) SPI flash
#                           and not linked (-DFX_CB_SPI, sp_cb.c, flash_host.c): -F IMAGE loads it
#   pager_host_cbi          v3.7, codebook in SPI flash, the table linked to install it (SP_CB_INSTALL)
#   cb_image                writes the codebook image (cb_image OUT.bin) or prints its CRC (-c)
#   pager_host_vlr          -DSP_LR -DSP_VLR: plays sotto 1200, 600 (0xAC) and 400 (0xAD) messages
#   pager_host_vlr_cbs      the same with both codebooks in (emulated) SPI flash and not linked
#                           (-F IMAGE for v3.7's at 0x100000, -V IMAGE for sotto 600 / 400's at 0x108000)
#   pager_host_vlr_cbi      the same with the sotto 600 / 400 tables linked to install them (SP_VLR_INSTALL)
#   vlr_image               writes the sotto 600 / 400 codebook image (vlr_image OUT.bin) or its CRC (-c)
#   pager_host_vlrh         -DSP_LR -DSP_VLR -DSP_HISS: sotto 1200, 600 and 400 at 16 kHz through the K6
#                           hiss layer (320-sample frames; docs/k6-hiss-port.md); the tables linked
# Always the v3.7 8 kHz core, whatever VARIANT the rest of c/ is built with: the packet
# format's magic 0xA7 means v3.7 frames, 0xA9 sotto 1200 (on the v3.7 core).
# SPDX-License-Identifier: MIT
CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
K6FLAGS = -std=c99 -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DFX_NARROW_MUL -DFX_CHECK -I.. -DFX_CS512_CONST -DFX_YIELD_HOOK
DECFLAGS = -Dcalloc=sp_calloc -Dfree=sp_free
OBJS = host/sotto_pager.o host/sotto_fx_dec.o host/sp_cb.o host/sp_vlr.o host/fx_math.o host/fx_tables.o

LR_OBJS = host-lr/sotto_pager.o host-lr/sotto_fx_dec.o host-lr/sotto_fx_lr.o host/sp_cb.o host/sp_vlr.o host/fx_math.o host/fx_tables.o

all: pager_host pager_host_lr km_host cb_image pager_host_cbs pager_host_lr_cbs pager_host_cbi \
	vlr_image pager_host_vlr pager_host_vlr_cbs pager_host_vlr_cbi pager_host_vlrh
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
# the codebook in SPI flash (sp_cb.h): cb_image from the table build of sp_cb.c, its CRC into
# host-cb/cb_crc.h for the builds without the table
cb_image: host/cb_image.o host/sp_cb.o host/fx_tables.o
	$(CC) -o $@ $^
host-cb/cb_crc.h: cb_image
	@mkdir -p host-cb
	echo "#define SP_CB_CRC $$(./cb_image -c)" > $@
CBFLAGS = -DFX_CB_SPI -include host-cb/cb_crc.h
CB_SRC = sotto_pager.c sp_cb.c flash_host.c pager_host.c
pager_host_cbs: $(CB_SRC:%.c=host-cbs/%.o) host-cbs/sotto_fx_dec.o host/sp_vlr.o host/fx_math.o host/fx_tables.o
	$(CC) -o $@ $^
pager_host_lr_cbs: $(CB_SRC:%.c=host-lrcbs/%.o) host-lrcbs/sotto_fx_dec.o host-lrcbs/sotto_fx_lr.o host/sp_vlr.o host/fx_math.o host/fx_tables.o
	$(CC) -o $@ $^
pager_host_cbi: $(CB_SRC:%.c=host-cbi/%.o) host-cbi/sotto_fx_dec.o host/sp_vlr.o host/fx_math.o host/fx_tables.o
	$(CC) -o $@ $^
host-cbs/%.o: %.c sotto_pager.h sp_cb.h host-cb/cb_crc.h
	@mkdir -p host-cbs
	$(CC) $(CFLAGS) $(K6FLAGS) $(CBFLAGS) -c -o $@ $<
host-cbs/%.o: ../fixed/%.c ../fixed/fx.h ../fixed/fx_tables.h host-cb/cb_crc.h
	@mkdir -p host-cbs
	$(CC) $(CFLAGS) $(K6FLAGS) $(CBFLAGS) $(DECFLAGS) -c -o $@ $<
host-lrcbs/%.o: %.c sotto_pager.h sp_cb.h host-cb/cb_crc.h
	@mkdir -p host-lrcbs
	$(CC) $(CFLAGS) $(K6FLAGS) $(CBFLAGS) -DSP_LR -c -o $@ $<
host-lrcbs/%.o: ../fixed/%.c ../fixed/fx.h ../fixed/fx_tables.h ../sotto_lr_dec.inc ../sotto_lr_tables.h host-cb/cb_crc.h
	@mkdir -p host-lrcbs
	$(CC) $(CFLAGS) $(K6FLAGS) $(CBFLAGS) $(DECFLAGS) -DSP_LR -c -o $@ $<
host-cbi/%.o: %.c sotto_pager.h sp_cb.h
	@mkdir -p host-cbi
	$(CC) $(CFLAGS) $(K6FLAGS) -DFX_CB_SPI -DSP_CB_INSTALL -c -o $@ $<
host-cbi/%.o: ../fixed/%.c ../fixed/fx.h ../fixed/fx_tables.h
	@mkdir -p host-cbi
	$(CC) $(CFLAGS) $(K6FLAGS) -DFX_CB_SPI -DSP_CB_INSTALL $(DECFLAGS) -c -o $@ $<
# sotto 600 / 400 (SP_VLR, on top of SP_LR): the tables linked (pager_host_vlr), both codebooks
# in SPI flash (_cbs; the sotto 600 / 400 image's CRC from vlr_image into host-cb/vlr_crc.h), or
# their tables linked to install them (_cbi)
VLRFLAGS = -DSP_LR -DSP_VLR
VLR_SRC = sotto_pager.c sp_cb.c sp_vlr.c flash_host.c pager_host.c
VLR_DEC = sotto_fx_dec.o sotto_fx_lr.o sotto_fx_vlr.o
vlr_image: host/vlr_image.o host-vlr/sp_vlr.o host/sp_cb.o host/fx_tables.o host-vlr/sotto_vlr_tables.o
	$(CC) -o $@ $^
host/vlr_image.o: vlr_image.c sp_vlr.h
	@mkdir -p host
	$(CC) $(CFLAGS) $(K6FLAGS) -c -o $@ $<
host-cb/vlr_crc.h: vlr_image
	@mkdir -p host-cb
	echo "#define SP_VLR_CRC $$(./vlr_image -c)" > $@
pager_host_vlr: host-vlr/pager_host.o host-vlr/sotto_pager.o host/sp_cb.o host-vlr/sp_vlr.o \
	$(VLR_DEC:%=host-vlr/%) host-vlr/sotto_vlr_tables.o host/fx_math.o host/fx_tables.o
	$(CC) -o $@ $^
pager_host_vlr_cbs: $(VLR_SRC:%.c=host-vlrcbs/%.o) $(VLR_DEC:%=host-vlrcbs/%) host-vlrcbs/sotto_vlr_tables.o \
	host/fx_math.o host/fx_tables.o
	$(CC) -o $@ $^
pager_host_vlr_cbi: $(VLR_SRC:%.c=host-vlrcbi/%.o) $(VLR_DEC:%=host-vlrcbi/%) host-vlrcbi/sotto_vlr_tables.o \
	host/fx_math.o host/fx_tables.o
	$(CC) -o $@ $^
VLR_HDRS = sotto_pager.h sp_cb.h sp_vlr.h ../sotto_vlr.h ../sotto_vlr_tables.h
VLR_FX_HDRS = ../fixed/fx.h ../fixed/fx_tables.h ../fixed/fx_internal.h ../sotto_lr_dec.inc ../sotto_lr_tables.h \
	../sotto_vlr.h ../sotto_vlr_tables.h ../sotto_vlr_dec.inc
host-vlr/%.o: %.c $(VLR_HDRS)
	@mkdir -p host-vlr
	$(CC) $(CFLAGS) $(K6FLAGS) $(VLRFLAGS) -c -o $@ $<
host-vlr/sotto_vlr_tables.o: ../sotto_vlr_tables.c ../sotto_vlr_tables.h
	@mkdir -p host-vlr
	$(CC) $(CFLAGS) $(K6FLAGS) -c -o $@ $<
host-vlr/%.o: ../fixed/%.c $(VLR_FX_HDRS)
	@mkdir -p host-vlr
	$(CC) $(CFLAGS) $(K6FLAGS) $(DECFLAGS) $(VLRFLAGS) -c -o $@ $<
# the hiss band build (SP_HISS on SP_LR SP_VLR): the 16 kHz decoders of c/fixed/sotto_fx_hiss.c
# (FX_HISS_FB, no encoder) on the v3.8 core; everything built with its flags
HISSFLAGS = $(VLRFLAGS) -DSP_HISS -DSOTTO_V38 -DFX_HISS_LIGHT -DFX_HISS_FB -DFX_HISS_NO_ENC
HISS_DEC = sotto_fx_dec.o sotto_fx_lr.o sotto_fx_vlr.o sotto_fx_hiss.o fx_math.o fx_tables.o
pager_host_vlrh: host-vlrh/pager_host.o host-vlrh/sotto_pager.o host-vlrh/sp_cb.o host-vlrh/sp_vlr.o \
	$(HISS_DEC:%=host-vlrh/%) host-vlrh/sotto_vlr_tables.o
	$(CC) -o $@ $^
host-vlrh/%.o: %.c $(VLR_HDRS)
	@mkdir -p host-vlrh
	$(CC) $(CFLAGS) $(K6FLAGS) $(HISSFLAGS) -c -o $@ $<
host-vlrh/sotto_vlr_tables.o: ../sotto_vlr_tables.c ../sotto_vlr_tables.h
	@mkdir -p host-vlrh
	$(CC) $(CFLAGS) $(K6FLAGS) $(HISSFLAGS) -c -o $@ $<
host-vlrh/%.o: ../fixed/%.c $(VLR_FX_HDRS) ../fixed/sotto_fx_hiss.c
	@mkdir -p host-vlrh
	$(CC) $(CFLAGS) $(K6FLAGS) $(DECFLAGS) $(HISSFLAGS) -c -o $@ $<
VCBSFLAGS = $(VLRFLAGS) $(CBFLAGS) -DVLR_CB_SPI -include host-cb/vlr_crc.h
host-vlrcbs/%.o: %.c $(VLR_HDRS) host-cb/cb_crc.h host-cb/vlr_crc.h
	@mkdir -p host-vlrcbs
	$(CC) $(CFLAGS) $(K6FLAGS) $(VCBSFLAGS) -c -o $@ $<
host-vlrcbs/sotto_vlr_tables.o: ../sotto_vlr_tables.c ../sotto_vlr_tables.h
	@mkdir -p host-vlrcbs
	$(CC) $(CFLAGS) $(K6FLAGS) -DVLR_CB_SPI -c -o $@ $<
host-vlrcbs/%.o: ../fixed/%.c $(VLR_FX_HDRS) host-cb/cb_crc.h host-cb/vlr_crc.h
	@mkdir -p host-vlrcbs
	$(CC) $(CFLAGS) $(K6FLAGS) $(VCBSFLAGS) $(DECFLAGS) -c -o $@ $<
VCBIFLAGS = $(VLRFLAGS) $(CBFLAGS) -DVLR_CB_SPI -DSP_VLR_INSTALL
host-vlrcbi/%.o: %.c $(VLR_HDRS) host-cb/cb_crc.h
	@mkdir -p host-vlrcbi
	$(CC) $(CFLAGS) $(K6FLAGS) $(VCBIFLAGS) -c -o $@ $<
host-vlrcbi/sotto_vlr_tables.o: ../sotto_vlr_tables.c ../sotto_vlr_tables.h
	@mkdir -p host-vlrcbi
	$(CC) $(CFLAGS) $(K6FLAGS) -c -o $@ $<
host-vlrcbi/%.o: ../fixed/%.c $(VLR_FX_HDRS) host-cb/cb_crc.h
	@mkdir -p host-vlrcbi
	$(CC) $(CFLAGS) $(K6FLAGS) $(VCBIFLAGS) $(DECFLAGS) -c -o $@ $<
clean:
	rm -rf host host-lr pager_host pager_host_lr km_host build build-lr \
	  host-cb host-cbs host-lrcbs host-cbi cb_image pager_host_cbs pager_host_lr_cbs pager_host_cbi \
	  host-vlr host-vlrcbs host-vlrcbi vlr_image pager_host_vlr pager_host_vlr_cbs pager_host_vlr_cbi host-vlrh pager_host_vlrh \
	  build-vlr build-vlr-cbs build-vlr-cbi
.PHONY: all clean
