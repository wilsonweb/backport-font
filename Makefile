PY := .venv/bin/python
PIP := .venv/bin/pip

INPUT  ?= /usr/share/fonts/google-noto-color-emoji-fonts/Noto-COLRv1.ttf
OUTPUT ?= build/output/NotoColorEmojiCompat.ttf
NAME   ?= Noto Color Emoji Compat
MODE   ?= auto

FONT_DIR := $(HOME)/.local/share/fonts

.PHONY: setup build dist install check clean inspect help

help:
	@echo "Targets:"
	@echo "  setup    Create venv and install dependencies"
	@echo "  build    Convert INPUT into OUTPUT (defaults to system Noto Color Emoji)"
	@echo "  dist     Refresh dist/ release artifacts (copies OUTPUT and OFL.txt)"
	@echo "  install  Copy OUTPUT to ~/.local/share/fonts and refresh fontconfig"
	@echo "  check    Report whether the source font has changed since last build"
	@echo "  inspect  Print name table and color tables of OUTPUT"
	@echo "  clean    Remove build/ artifacts"
	@echo ""
	@echo "Variables (override with make VAR=value <target>):"
	@echo "  INPUT  = $(INPUT)"
	@echo "  OUTPUT = $(OUTPUT)"
	@echo "  NAME   = $(NAME)"
	@echo "  MODE   = $(MODE)  (auto|rename-only|colr-v0)"

setup:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

build:
	$(PY) convert.py --input "$(INPUT)" --output "$(OUTPUT)" --name "$(NAME)" --mode "$(MODE)"
	$(PY) -c "from fontTools.ttLib import TTFont; f=TTFont('$(OUTPUT)'); \
	  print('source version:', f['name'].getName(5,3,1,0x409).toUnicode() if f['name'].getName(5,3,1,0x409) else '?')" \
	  | tee build/last-built-version.txt

dist: build
	mkdir -p dist
	cp "$(OUTPUT)" dist/
	cp OFL.txt dist/
	@echo "Refreshed dist/ — commit these to publish a new release."

install: build
	mkdir -p $(FONT_DIR)
	cp "$(OUTPUT)" $(FONT_DIR)/
	fc-cache -f $(FONT_DIR)
	@echo "Installed. fc-list snippet:"
	@fc-list | grep -F "$(NAME)" || echo "  (not found — fontconfig may need a moment)"

check:
	$(PY) scripts/check_upstream.py --source "$(INPUT)"

inspect:
	$(PY) scripts/inspect_font.py "$(OUTPUT)"

clean:
	rm -rf build/
