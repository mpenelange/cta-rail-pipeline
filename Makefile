.PHONY: test ingest tui
test:
	uv run python -m unittest discover -s tests -v
ingest:
	uv run cta-pipeline ingest
tui:
	uv run cta-pipeline
