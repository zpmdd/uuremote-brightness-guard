SWIFTC := /usr/bin/xcrun swiftc
SDK := $(shell /usr/bin/xcrun --sdk macosx --show-sdk-path)
HELPER := bin/DisplayBrightnessTool
PLIST := launchd/io.github.zpmdd.uuremote-brightness-guard.plist

.PHONY: build lint test install status restore uninstall

build:
	/bin/mkdir -p bin
	$(SWIFTC) -O -sdk "$(SDK)" \
		-import-objc-header src/Bridging-Header.h \
		-framework AppKit -framework CoreGraphics -framework IOKit \
		-F /System/Library/PrivateFrameworks -framework DisplayServices \
		src/DisplayBrightnessTool.swift -o $(HELPER)

lint:
	/usr/bin/plutil -lint $(PLIST)
	/usr/bin/python3 -m py_compile uuremote_brightness_guard.py
	@for script in *.sh *.command scripts/*.sh tests/*.zsh; do /bin/zsh -n "$$script"; done

test:
	/usr/bin/python3 -m unittest discover -s tests -v
	./tests/test_plist_render.zsh

install:
	./install.sh

status:
	./status.sh

restore:
	./restore.sh

uninstall:
	./uninstall.sh
