# WebAssembly build of the sotto codecs for the listening page (docs/index.html): v3.8 (2400),
# sotto 1200 hiss, and sotto 600 / 400 (float encoder in sotto.wasm, fixed-point decoder on the
# v3.7 core in sotto_vlr.wasm, the same code the K6 plays).
# Needs clang and wasm-ld with a wasm32-wasi sysroot (Debian/Ubuntu: apt install clang lld
# wasi-libc libclang-rt-dev-wasm32). Run `make` in the repository root first: build/codec2 holds
# Codec 2's generated codebooks (sotto's own tables are committed in c/).
#   make            sotto.wasm and sotto_vlr.wasm
#   make install    copies them to docs/ (the files the page loads)
#   make check      runs both under node against the native tools (c/sotto_enc, c/sotto_dec and
#                   a VARIANT=v3.7 c/fixed/sotto_vlr_dec_fx, built in ../../build/c37 if missing)
#   make sotto_fx.wasm   the fixed-point codec (c/fixed) behind the same entry points
CC_WASM ?= clang
WASI_SYSROOT ?=
C2SRC = ../../codec/codec2/src
C2GEN = ../../build/codec2
TARGET = --target=wasm32-wasi $(if $(WASI_SYSROOT),--sysroot=$(WASI_SYSROOT))
CFLAGS = $(TARGET) -O2 -std=gnu99 -ffp-contract=off -ffunction-sections -fdata-sections \
         -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DSOTTO_V38 -I.. -I$(C2SRC) -I$(C2GEN) -I$(C2GEN)/codec2 -w
# Codec 2 files the decoder reaches (unused code is dropped by --gc-sections)
C2FILES = codec2.c codec2_fft.c kiss_fft.c kiss_fftr.c sine.c nlp.c phase.c postfilter.c \
          quantise.c lpc.c lsp.c interp.c newamp1.c mbest.c pack.c
C2GENFILES = codebook.c codebookd.c codebookjmv.c codebookge.c codebooknewamp1.c \
             codebooknewamp1_energy.c codebooknewamp2.c codebooknewamp2_energy.c
SRCS = sotto_wasm.c ../sotto.c ../sotto_hiss.c ../sotto_codebook.c ../sotto_hiss_tables.c \
       ../sotto_lr.c ../sotto_vlr.c ../sotto_vlr_dec.c ../sotto_vlr_tables.c \
       $(addprefix $(C2SRC)/,$(C2FILES)) $(addprefix $(C2GEN)/src/,$(C2GENFILES))
OBJS = $(patsubst %.c,obj/%.o,$(notdir $(SRCS)))
vpath %.c . .. $(C2SRC) $(C2GEN)/src

all: sotto.wasm sotto_vlr.wasm

sotto.wasm: $(OBJS)
	$(CC_WASM) $(TARGET) -mexec-model=reactor -Wl,--gc-sections -Wl,--strip-all \
	  -Wl,-z,stack-size=1048576 -Wl,--initial-memory=8388608 -Wl,--max-memory=268435456 \
	  -o $@ $(OBJS)

obj/%.o: %.c | obj
	$(CC_WASM) $(CFLAGS) -c -o $@ $<
# sotto_wasm.c supplies codec2_rand with a 64-bit state (see there)
obj/sine.o: CFLAGS += -Dcodec2_rand=codec2_rand_32bit
obj:
	mkdir -p obj

# the fixed-point codec (c/fixed, as on the C5517) with the same entry points: sotto_fx.wasm,
# for comparing fixed-point versions in the browser
FX_SRCS = sotto_wasm.c ../fixed/fx_math.c ../fixed/fx_tables.c ../fixed/sotto_fx_enc.c \
          ../fixed/sotto_fx_dec.c ../fixed/sotto_fx_hiss.c
FX_OBJS = $(patsubst %.c,objfx/%.o,$(notdir $(FX_SRCS)))
FX_CFLAGS = $(TARGET) -O2 -std=gnu99 -ffunction-sections -fdata-sections -DSOTTO_FX \
            -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DSOTTO_V38 -I.. -I../fixed -w

sotto_fx.wasm: $(FX_OBJS)
	$(CC_WASM) $(TARGET) -mexec-model=reactor -Wl,--gc-sections -Wl,--strip-all \
	  -Wl,-z,stack-size=1048576 -Wl,--initial-memory=8388608 -Wl,--max-memory=268435456 \
	  -o $@ $(FX_OBJS)

objfx/%.o: %.c | objfx
	$(CC_WASM) $(FX_CFLAGS) -c -o $@ $<
objfx/%.o: ../fixed/%.c | objfx
	$(CC_WASM) $(FX_CFLAGS) -c -o $@ $<
objfx:
	mkdir -p objfx

# sotto 600 / 400 decoder: fixed point on the v3.7 core (no SOTTO_V38), as sotto_vlr_dec_fx and
# the K6 pager build it
VLR_SRCS = sotto_wasm.c ../fixed/fx_math.c ../fixed/fx_tables.c ../fixed/sotto_fx_dec.c ../fixed/sotto_fx_vlr.c \
           ../sotto_vlr_tables.c
VLR_OBJS = $(patsubst %.c,objvlr/%.o,$(notdir $(VLR_SRCS)))
VLR_CFLAGS = $(TARGET) -O2 -std=gnu99 -ffunction-sections -fdata-sections -DSOTTO_WASM_VLR \
             -DSOTTO_V36 -DSOTTO_PITCH_THIRD -I.. -I../fixed -w

sotto_vlr.wasm: $(VLR_OBJS)
	$(CC_WASM) $(TARGET) -mexec-model=reactor -Wl,--gc-sections -Wl,--strip-all \
	  -Wl,-z,stack-size=262144 -Wl,--initial-memory=2097152 -Wl,--max-memory=67108864 \
	  -o $@ $(VLR_OBJS)

objvlr/%.o: %.c | objvlr
	$(CC_WASM) $(VLR_CFLAGS) -c -o $@ $<
objvlr/%.o: ../fixed/%.c | objvlr
	$(CC_WASM) $(VLR_CFLAGS) -c -o $@ $<
objvlr/%.o: ../%.c | objvlr
	$(CC_WASM) $(VLR_CFLAGS) -c -o $@ $<
objvlr:
	mkdir -p objvlr

install: sotto.wasm sotto_vlr.wasm
	cp sotto.wasm ../../docs/sotto.wasm
	cp sotto_vlr.wasm ../../docs/sotto_vlr.wasm

# the native 600 / 400 reference decoder is a VARIANT=v3.7 fixed build (c/ itself is v3.8)
C37 = ../../build/c37
$(C37)/fixed/sotto_vlr_dec_fx:
	rm -rf $(C37) && mkdir -p $(C37) && cp -r ../*.c ../*.h ../*.inc ../Makefile ../fixed $(C37)/
	$(MAKE) -C $(C37) clean
	$(MAKE) -C $(C37) VARIANT=v3.7 fixed/sotto_vlr_dec_fx

check: sotto.wasm sotto_vlr.wasm $(C37)/fixed/sotto_vlr_dec_fx
	cd ../.. && node c/wasm/check_wasm.mjs c/wasm/sotto.wasm c/wasm/sotto_vlr.wasm

clean:
	rm -rf obj objfx objvlr sotto.wasm sotto_fx.wasm sotto_vlr.wasm
.PHONY: all install check clean
