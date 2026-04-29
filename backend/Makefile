# SyncSonic backend Makefile
# Slice 1: reproducible measurement session entry points.
#
# All targets are expected to be run from the backend directory on the
# Pi (or anywhere with the same Python module layout). They invoke
# python with the repo cwd so `python -m measurement.run_session`
# resolves correctly.

PYTHON ?= python3
NAME ?= adhoc
DURATION ?= 30

.PHONY: help session report session-and-report compile

help:
	@echo "SyncSonic Slice 1 measurement targets:"
	@echo ""
	@echo "  make session NAME=<name> [DURATION=30]"
	@echo "      Play the reference signal for DURATION seconds, snapshot the"
	@echo "      live events jsonl + the rolling mic capture into a session"
	@echo "      bundle at \$$SYNCSONIC_TELEMETRY_ROOT/sessions/<name>-<ts>/."
	@echo ""
	@echo "  make report [BUNDLE=path]"
	@echo "      Render report.md from the named session bundle (or the"
	@echo "      sessions/latest symlink if BUNDLE is omitted)."
	@echo ""
	@echo "  make session-and-report NAME=<name> [DURATION=30]"
	@echo "      Convenience: run a session then immediately render the"
	@echo "      report for it. The standard `make session NAME=test1` flow."
	@echo ""
	@echo "  make compile"
	@echo "      python -m compileall syncsonic_ble + measurement (CI sanity)."

session:
	$(PYTHON) -m measurement.run_session --name $(NAME) --duration $(DURATION)

report:
ifdef BUNDLE
	$(PYTHON) -m measurement.report --bundle $(BUNDLE)
else
	$(PYTHON) -m measurement.report
endif

session-and-report:
	$(PYTHON) -m measurement.run_session --name $(NAME) --duration $(DURATION)
	$(PYTHON) -m measurement.report

compile:
	$(PYTHON) -m compileall syncsonic_ble measurement
