.DELETE_ON_ERROR:
DUCKDB_VERSION ?= 1.5.6
BUILD ?= build
DEPS ?= $(abspath $(BUILD)/deps/$(DUCKDB_VERSION))
DUCKDB ?= $(DEPS)/duckdb
PYTHON ?= python3
CXXFLAGS ?= -O2
FLAGS = -std=c++17 -Wall -Wextra -Werror -Wno-missing-field-initializers -fPIC -fvisibility=hidden
ifeq ($(shell uname -s),Darwin)
SHARED = -dynamiclib
else
SHARED = -shared
endif

.PHONY: all fetch test test-sql package clean
all: $(BUILD)/whereabouts.duckdb_extension
fetch:
	bash scripts/fetch-duckdb.sh $(DEPS) $(DUCKDB_VERSION)
$(BUILD):
	mkdir -p $@
$(BUILD)/build-tool: scripts/build-tool.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(FLAGS) $< -o $@
$(BUILD)/whereabouts_sql.hpp: sql/whereabouts.sql $(BUILD)/build-tool
	$(BUILD)/build-tool sql $< $@
$(BUILD)/whereabouts.so: src/whereabouts.cpp $(BUILD)/whereabouts_sql.hpp vendor/duckdb/duckdb_extension.h vendor/duckdb/duckdb.h
	$(CXX) $(CXXFLAGS) $(FLAGS) $(SHARED) -Ivendor/duckdb -I$(BUILD) -DDUCKDB_EXTENSION_NAME=whereabouts $< -o $@
$(BUILD)/whereabouts.duckdb_extension: $(BUILD)/whereabouts.so $(BUILD)/build-tool
	$(BUILD)/build-tool metadata $< $@ "$$($(DUCKDB) -no-stdin -csv -noheader -c 'PRAGMA platform;')" 0.1.0
test: all
	$(PYTHON) tests/test_extension.py --duckdb $(DUCKDB) --extension $(abspath $(BUILD)/whereabouts.duckdb_extension)
test-sql:
	$(PYTHON) tests/test_extension.py --duckdb $(DUCKDB) --sql sql/whereabouts.sql
package: all
	tar -czf $(BUILD)/whereabouts-0.1.0-$$(uname -s)-$$(uname -m).tar.gz -C $(BUILD) whereabouts.duckdb_extension -C $(CURDIR) README.md LICENSE THIRD_PARTY_NOTICES.md licenses sql examples vendor/duckdb/LICENSE
clean:
	rm -f $(BUILD)/build-tool $(BUILD)/whereabouts_sql.hpp $(BUILD)/whereabouts.so $(BUILD)/whereabouts.duckdb_extension
