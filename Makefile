SWIFTC := /usr/bin/xcrun swiftc
SDK := $(shell /usr/bin/xcrun --sdk macosx --show-sdk-path)
HELPER := bin/DisplayBrightnessTool
PLIST := launchd/io.github.zpmdd.uuremote-brightness-guard.plist
SWIFT_FLAGS := -sdk "$(SDK)" -import-objc-header src/Bridging-Header.h \
	-framework AppKit -framework CoreGraphics -framework IOKit \
	-F /System/Library/PrivateFrameworks -framework DisplayServices

.PHONY: build lint test test-ddc install status restore uninstall

build:
	/bin/mkdir -p bin
	$(SWIFTC) -O $(SWIFT_FLAGS) \
		src/DisplayBrightnessTool.swift -o $(HELPER)

lint:
	/usr/bin/plutil -lint $(PLIST)
	/usr/bin/python3 -m py_compile uuremote_brightness_guard.py
	@for script in *.sh *.command scripts/*.sh tests/*.zsh; do /bin/zsh -n "$$script"; done

test: test-ddc
	/usr/bin/python3 -m unittest discover -s tests -v
	./tests/test_plist_render.zsh

test-ddc:
	/bin/mkdir -p .build
	$(SWIFTC) $(SWIFT_FLAGS) -D DDC_REPLY_SELF_TEST \
		src/DisplayBrightnessTool.swift -o .build/DDCReplyChecks
	./.build/DDCReplyChecks

install:
	./install.sh

status:
	./status.sh

restore:
	./restore.sh

uninstall:
	./uninstall.sh
