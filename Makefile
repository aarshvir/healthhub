.PHONY: help install install-dev test cov build example install-wheel clean

help:
	@echo "healthhub — make targets:"
	@echo "  install       install pinned runtime+test deps (requirements.txt)"
	@echo "  install-dev   editable install with dev extras"
	@echo "  test          run the full test suite (incl. architecture enforcement)"
	@echo "  build         build sdist + wheel into dist/"
	@echo "  install-wheel build, then install the wheel and smoke-test the CLI"
	@echo "  example       regenerate the sample CSV and write metrics.json"
	@echo "  clean         remove build/test artifacts"

install:
	pip install -r requirements.txt

install-dev:
	pip install -e ".[dev]"

test:
	pytest

cov:
	pytest --maxfail=1

build: clean
	python -m build

install-wheel: build
	pip install --no-deps --force-reinstall dist/*.whl
	healthhub-metrics examples/glucose_sample.csv -o metrics.json --walk-adherence 0.8

example:
	python examples/generate_sample.py
	healthhub-metrics examples/glucose_sample.csv -o metrics.json --walk-adherence 0.8

clean:
	rm -rf dist build *.egg-info .pytest_cache .hypothesis
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
