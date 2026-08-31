.PHONY: test ingest serve
test:
	uv run python -m unittest discover -s tests -v
ingest:
	uv run cta-pipeline ingest
serve:
	uv run cta-pipeline serve
