# WebAssembly build of the sotto v3.8 decoder for the listening page (docs/index.html).
# Needs clang and wasm-ld with a wasm32-wasi sysroot (Debian/Ubuntu: apt install clang lld
# wasi-libc libclang-rt-dev-wasm32). Run `make` in the repository root first: build/codec2 holds
# Codec 2's generated codebooks (sotto's own tables are committed in c/).
#   make            sotto.wasm
#   make install    copies it to docs/sotto.wasm (the file the page loads)
#   make check      decodes with node and compares against c/sotto_dec -16
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
       $(addprefix $(C2SRC)/,$(C2FILES)) $(addprefix $(C2GEN)/src/,$(C2GENFILES))
OBJS = $(patsubst %.c,obj/%.o,$(notdir $(SRCS)))
vpath %.c . .. $(C2SRC) $(C2GEN)/src

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

install: sotto.wasm
	cp sotto.wasm ../../docs/sotto.wasm

check: sotto.wasm
	cd ../.. && node c/wasm/check_wasm.mjs

clean:
	rm -rf obj objfx sotto.wasm sotto_fx.wasm
.PHONY: install check clean
