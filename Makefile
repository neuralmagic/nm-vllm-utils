CHECKDIRS := nm_vllm_utils nm_vllm_utils_test

# style the code according to accepted standards for the repo
style:
	pre-commit run --all-files -c .pre-commit-config.yaml

test-with-coverage:
	coverage run -m pytest
	coverage report | tee .meta/coverage/report.txt
	coverage-badge -f -o ./.meta/coverage/badge.svg
