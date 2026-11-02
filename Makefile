# sotto top-level build. Builds our Codec 2 fork, then libsotto and its tools in c/.
#   make                          Codec 2 (build/codec2) + libsotto.so, libsotto.a, c/sotto_enc, c/sotto_dec
#   make fixed                    the fixed-point (integer-only) encoder and decoder in c/fixed/
#   make check                    C API test, the generated-table check, then the Python tests (pytest)
#   make tables                   regenerate the committed C tables from sim/data/*.npz (numpy, scipy)
#   make check-tables             fail if the committed C tables differ from what the generators write
#   make install PREFIX=/usr/local   libsotto + header + pkg-config, and libcodec2 from our fork
#   make clean                    remove the C build;  make distclean also removes build/codec2
# Variables go through to c/Makefile: VARIANT=v3.5, CODEBOOK=..., PREFIX, LIBDIR, INCDIR, DESTDIR.
# The default build needs no Python: the generated C tables are committed. PYTHON=.venv/bin/python
# picks the Python for make tables, make check-tables, make check and the VARIANT=v3.5 codebook.
PREFIX ?= /usr/local
PYTHON ?= python3
CODEC2_LIB = build/codec2/src/libcodec2.so

all: lib

codec2: $(CODEC2_LIB)
$(CODEC2_LIB):
	sh tools/build_codec2.sh

lib: $(CODEC2_LIB)
	$(MAKE) -C c

fixed: lib
	$(MAKE) -C c fixed

check: lib fixed
	$(MAKE) -C c check
	$(MAKE) -C c check-tables PYTHON=$(PYTHON)
	$(PYTHON) -m pytest -q tests

tables check-tables:
	$(MAKE) -C c $@ PYTHON=$(PYTHON)

install: lib
	$(MAKE) -C c install
	DESTDIR=$(DESTDIR) cmake --install build/codec2 --prefix $(PREFIX)

clean:
	$(MAKE) -C c clean

distclean: clean
	rm -rf build/codec2

.PHONY: all codec2 lib fixed check tables check-tables install clean distclean
