# Simplified investigation experiment

This folder is an isolated prototype. It is not connected to Streamlit or the production runtime.

Run deterministic tests from the parent directory:

```powershell
python -m pytest simplified_exp/test_experiment.py -q
```

Run one live investigation with the OpenAI API:

```powershell
python simplified_exp/run_experiment.py "SE100016 has a passenger vehicle outside commercial vehicle financing."
```

A screenshot can be supplied with `--image path/to/screenshot.png`.

The parser reads only the latest investigation input. The Python verifier reuses the existing resolver and scoring implementation, and the generator receives only the resulting ResponseContext.

Run the live test set from the parent directory:

`powershell
python simplified_exp\run_test_set.py --limit 1
` 

Use --limit 2 for a short check. The command prints each parser request, score result, Mikael reply, latency, and a final failure summary.
