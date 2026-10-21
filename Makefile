CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror
UNAME := $(shell uname -s)
ifeq ($(UNAME),Linux)
LDLIBS := -lutil
endif

build/crow-session: src/crow-session.c
	mkdir -p build
	$(CC) $(CFLAGS) $< -o $@ $(LDLIBS)

build: build/crow-session

test: build
	CROWCLI_NO_IME_SWITCH=1 python3 -m unittest discover -s tests -v

clean:
	rm -rf build

.PHONY: build test clean
