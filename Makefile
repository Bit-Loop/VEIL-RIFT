CC       ?= gcc
CFLAGS   ?= -O3 -Wall -Wextra -fPIC
LDFLAGS  ?= -shared -lX11 -lepoxy -lmpv -lvulkan -lm
GLSLANG  ?= glslangValidator

PREFIX_BIN   ?= $(HOME)/.local/bin
PREFIX_SHARE ?= $(HOME)/.local/share/wallpaper-mode
BUILD_DIR    := build

SO_TARGET  := $(BUILD_DIR)/corrupt_gl_renderer.so
SPV_TARGET := $(BUILD_DIR)/corrupt-rt5070.spv

.PHONY: all validate clean install

all: validate $(SPV_TARGET) $(SO_TARGET)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

validate:
	$(GLSLANG) -S frag shaders/corrupt-life-step.frag
	$(GLSLANG) -S frag shaders/corrupt-veil.frag

$(SPV_TARGET): shaders/corrupt-rt5070.comp | $(BUILD_DIR)
	$(GLSLANG) --target-env vulkan1.3 -V shaders/corrupt-rt5070.comp -o $(SPV_TARGET)

$(SO_TARGET): src/corrupt_gl_renderer.c src/vk_rt5070.c src/vk_rt5070.h | $(BUILD_DIR)
	$(CC) $(CFLAGS) -Isrc src/corrupt_gl_renderer.c src/vk_rt5070.c -o $(SO_TARGET) $(LDFLAGS)

install: all
	mkdir -p "$(PREFIX_BIN)" "$(PREFIX_SHARE)"
	install -m 0755 bin/wallpaper-mode "$(PREFIX_BIN)/wallpaper-mode"
	install -m 0755 bin/video-wallpaper.py "$(PREFIX_BIN)/video-wallpaper.py"
	install -m 0755 bin/corrupt_lockin_engine.py "$(PREFIX_BIN)/corrupt_lockin_engine.py"
	install -m 0755 bin/wallpaper-mode-tray.py "$(PREFIX_BIN)/wallpaper-mode-tray.py"
	install -m 0755 bin/nasa-apod-wallpaper.sh "$(PREFIX_BIN)/nasa-apod-wallpaper.sh"
	install -m 0644 src/corrupt_gl_renderer.c "$(PREFIX_SHARE)/corrupt_gl_renderer.c"
	install -m 0644 src/vk_rt5070.c "$(PREFIX_SHARE)/vk_rt5070.c"
	install -m 0644 src/vk_rt5070.h "$(PREFIX_SHARE)/vk_rt5070.h"
	install -m 0644 shaders/corrupt-veil.frag "$(PREFIX_SHARE)/corrupt-veil.frag"
	install -m 0644 shaders/corrupt-life-step.frag "$(PREFIX_SHARE)/corrupt-life-step.frag"
	install -m 0644 shaders/corrupt-rt5070.comp "$(PREFIX_SHARE)/corrupt-rt5070.comp"
	install -m 0644 $(SPV_TARGET) "$(PREFIX_SHARE)/corrupt-rt5070.spv"
	install -m 0755 $(SO_TARGET) "$(PREFIX_SHARE)/corrupt_gl_renderer.so"
	install -m 0644 assets/icons/wallpaper-*.svg "$(PREFIX_SHARE)/"

clean:
	rm -rf $(BUILD_DIR)
