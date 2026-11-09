# Host-side tests and demo (firmware is built with idf.py)
CC     ?= gcc
CFLAGS  = -std=c11 -Wall -Wextra -Werror -g -fsanitize=address,undefined -Imain
# Use the project venv when there is one (pip install -r requirements.txt into .venv)
PY     ?= $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,python3)

.PHONY: test test-c test-py demo gui-demo record gui clean

test: test-c test-py

test-c: build-host/test_core
	./build-host/test_core

build-host/test_core: test/test_core.c main/csi_core.h
	@mkdir -p build-host
	$(CC) $(CFLAGS) test/test_core.c -o $@ -lm

build-host/score_lines: test/score_lines.c main/csi_core.h
	@mkdir -p build-host
	$(CC) $(CFLAGS) test/score_lines.c -o $@ -lm

test-py: build-host/score_lines
	cd tools && $(PY) test_pipeline.py && $(PY) test_probe_report.py && \
		$(PY) test_gui.py && $(PY) test_record.py && $(PY) test_history.py

demo:
	$(PY) tools/fake_csi.py | $(PY) tools/analyze.py -

record:          # log motion with clock times until Ctrl+C (e.g. overnight)
	$(PY) tools/record.py

gui:             # live GUI on the board
	$(PY) tools/gui.py $$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1)

gui-demo:
	$(PY) tools/fake_csi.py --realtime --loop | $(PY) tools/gui.py -

clean:
	rm -rf build-host
