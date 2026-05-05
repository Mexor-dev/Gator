GATOR_ROOT := $(abspath ../..)
BUILD_DIR := $(GATOR_ROOT)/build
TARGET_SO := $(GATOR_ROOT)/src/inference/libgator_kern.so

.PHONY: all clean

all:
	cmake -S $(GATOR_ROOT) -B $(BUILD_DIR)
	cmake --build $(BUILD_DIR) --target gator_kern
	@if [ -f "$(BUILD_DIR)/src/inference/libgator_kern.so" ]; then \
		cp "$(BUILD_DIR)/src/inference/libgator_kern.so" "$(TARGET_SO)"; \
	elif [ -f "$(BUILD_DIR)/libgator_kern.so" ]; then \
		cp "$(BUILD_DIR)/libgator_kern.so" "$(TARGET_SO)"; \
	else \
		echo "[FATAL] gator_kern build completed but shared object not found"; \
		exit 1; \
	fi

clean:
	rm -f "$(TARGET_SO)"
