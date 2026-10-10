# Expected renders

The 30 EOS startup configs and 13 FRR stand-in scripts that the templates and object files of this repo render for DC3.
`tests/test_dc3_standalone.py` renders them again from a fake store and compares, and `scripts/dc3_render_diff.py`
compares a render from a running Infrahub (bootstrap step 10). Any change to a template or to the data must explain
every line it changes against these files.
