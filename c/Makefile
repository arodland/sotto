# sotto C port. Links Codec 2 (LGPL-2.1) built in ../build/codec2 from ../codec/codec2.
#   make                 libsotto.so, libsotto.a, sotto_enc and sotto_dec
#   make check           API test against the shared library
#   make install PREFIX=/usr/local   library, header and pkg-config file
#   make CODEC2_SRC=... CODEC2_LIB=...   to use another Codec 2 tree
#   make clean all VARIANT=v3.7   for the 8 kHz v3.7 library (VARIANT=v3.6, v3.5: older variants)
#   make clean all RULE_LAG=5     v3.8 encoder look-ahead in frames (default 10)
#   make clean all CODEBOOK=sim/data/shape_msvq_9988.npz   for another codebook
CODEC2_SRC ?= ../codec/codec2/src
CODEC2_LIB ?= ../build/codec2/src
PYTHON ?= python3
# VARIANT=v3.6: 9+9+8+8 codebook in 8-bit values, energy from the rate-K envelope
# VARIANT=v3.7: v3.6 plus a pitch candidate at a third of Codec 2's estimate (encoder only,
#               same frames as v3.6)
# VARIANT=v3.8: v3.7 plus the 16 kHz hiss band (sotto_hiss.c, sim/hiss_band.py)
VARIANT ?= v3.8
ifeq ($(VARIANT),v3.8)
CODEBOOK ?= sim/data/shape_msvq_9988.npz
CBROUND = --round 8
CFLAGS_VARIANT = -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DSOTTO_V38
ifdef RULE_LAG
CFLAGS_VARIANT += -DSOTTO_RULE_LAG=$(RULE_LAG)
endif
HISS_OBJS = sotto_hiss.o sotto_hiss_tables.o
else ifeq ($(VARIANT),v3.7)
CODEBOOK ?= sim/data/shape_msvq_9988.npz
CBROUND = --round 8
CFLAGS_VARIANT = -DSOTTO_V36 -DSOTTO_PITCH_THIRD
else ifeq ($(VARIANT),v3.6)
CODEBOOK ?= sim/data/shape_msvq_9988.npz
CBROUND = --round 8
CFLAGS_VARIANT = -DSOTTO_V36
else
CODEBOOK ?= sim/data/shape_msvq_34.npz
endif
PREFIX ?= /usr/local
LIBDIR ?= $(PREFIX)/lib
INCDIR ?= $(PREFIX)/include

MAJOR = 0
VERSION = 0.1.0
SONAME = libsotto.so.$(MAJOR)

CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
CFLAGS += $(CFLAGS_VARIANT) -std=c99 -ffp-contract=off -fPIC -fvisibility=hidden -I$(CODEC2_SRC) -I$(CODEC2_LIB)/.. -I.
# the build-tree Codec 2 is found through an rpath; installed copies use the system path
C2LIBS = -L$(CODEC2_LIB) -Wl,-rpath,$(abspath $(CODEC2_LIB)) -lcodec2 -lm

all: libsotto.so libsotto.a sotto_enc sotto_dec

# fixed-point build (fixed/), integer only, v3.6 codebook; FX_CHECK aborts if a sum would
# overflow the C55x's 40-bit accumulator
FX_CODEBOOK = sim/data/shape_msvq_9988.npz
FXFLAGS ?= -DFX_CHECK
FX_OBJS = fixed/fx_math.o fixed/fx_tables.o
fixed: fixed/sotto_dec_fx fixed/sotto_enc_fx
fixed/fx_tables.c: ../$(FX_CODEBOOK) ../tools/gen_fx_tables.py
	cd .. && $(PYTHON) tools/gen_fx_tables.py --codebook $(FX_CODEBOOK) c/fixed/fx_tables.c
fixed/%.o: fixed/%.c fixed/fx.h fixed/fx_tables.h sotto.h
	$(CC) $(CFLAGS) $(FXFLAGS) -c -o $@ $<
fixed/sotto_dec_fx: sotto_dec.o fixed/sotto_fx_dec.o $(FX_OBJS)
	$(CC) -o $@ $^
fixed/sotto_enc_fx: sotto_enc.o fixed/sotto_fx_enc.o $(FX_OBJS)
	$(CC) -o $@ $^

sotto_codebook.c: ../$(CODEBOOK) ../tools/export_codebook_c.py
	cd .. && $(PYTHON) tools/export_codebook_c.py $(CBROUND) $(CODEBOOK) c/sotto_codebook.c

sotto_hiss_tables.c: ../sim/data/hiss_band.npz ../sim/data/hiss_vowel_fine.npz ../tools/export_hiss_c.py
	cd .. && $(PYTHON) tools/export_hiss_c.py c/sotto_hiss_tables.c

sotto.o: sotto.c sotto.h sotto_codebook.h sotto_internal.h
sotto_hiss.o: sotto_hiss.c sotto.h sotto_internal.h sotto_hiss_tables.h
sotto_hiss_tables.o: sotto_hiss_tables.c sotto_hiss_tables.h
sotto_codebook.o: sotto_codebook.c sotto_codebook.h

libsotto.a: sotto.o sotto_codebook.o $(HISS_OBJS)
	$(AR) rcs $@ $^

libsotto.so.$(VERSION): sotto.o sotto_codebook.o $(HISS_OBJS)
	$(CC) -shared -Wl,-soname,$(SONAME) -o $@ $^ $(C2LIBS)
libsotto.so: libsotto.so.$(VERSION)
	ln -sf $< $(SONAME)
	ln -sf $< $@

sotto_enc: sotto_enc.o libsotto.a
	$(CC) -o $@ $^ $(C2LIBS)
sotto_dec: sotto_dec.o libsotto.a
	$(CC) -o $@ $^ $(C2LIBS)

test_api: test_api.o libsotto.so
	$(CC) -o $@ test_api.o -L. -Wl,-rpath,$(abspath .) -lsotto -lm

check: test_api
	./test_api
	@exported=$$(nm -D --defined-only libsotto.so | awk '$$2 == "T" || $$2 == "D" || $$2 == "R" {print $$3}' | grep -v '^sotto_' || true); \
	if [ -n "$$exported" ]; then echo "unexpected exports: $$exported"; exit 1; fi; \
	echo "exports: only sotto_*"

sotto.pc: sotto.pc.in
	sed -e 's|@PREFIX@|$(PREFIX)|' -e 's|@LIBDIR@|$(LIBDIR)|' -e 's|@INCDIR@|$(INCDIR)|' \
	    -e 's|@VERSION@|$(VERSION)|' $< > $@

install: libsotto.so libsotto.a sotto.pc
	install -d $(DESTDIR)$(LIBDIR)/pkgconfig $(DESTDIR)$(INCDIR)
	install -m 755 libsotto.so.$(VERSION) $(DESTDIR)$(LIBDIR)/
	ln -sf libsotto.so.$(VERSION) $(DESTDIR)$(LIBDIR)/$(SONAME)
	ln -sf libsotto.so.$(VERSION) $(DESTDIR)$(LIBDIR)/libsotto.so
	install -m 644 libsotto.a $(DESTDIR)$(LIBDIR)/
	install -m 644 sotto.h $(DESTDIR)$(INCDIR)/
	install -m 644 sotto.pc $(DESTDIR)$(LIBDIR)/pkgconfig/

clean:
	rm -f *.o libsotto.a libsotto.so* sotto_enc sotto_dec test_api sotto_codebook.c sotto_hiss_tables.c sotto.pc
	rm -f fixed/*.o fixed/fx_tables.c fixed/sotto_dec_fx fixed/sotto_enc_fx

.PHONY: all fixed check install clean
