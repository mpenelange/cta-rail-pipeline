.PHONY: test compile demo serve clean

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

compile:
	python3 -m compileall -q src tests

demo:
	PYTHONPATH=src python3 -m cta_pipeline demo

serve:
	PYTHONPATH=src python3 -m cta_pipeline serve

clean:
	find src tests -type d -name __pycache__ -prune -exec rm -r {} +
