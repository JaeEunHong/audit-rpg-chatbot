# Refactoring overwrite map

The files under `refactoring/` are an isolated implementation and do not modify
the existing application.

Before applying them to the application, run the unit and replay tests. Then
copy the new `main/core` modules into the target tree and update the application
entry point deliberately. Do not overwrite `audit_rpg.py` or `run_experiment.py`
automatically; they still contain legacy integration code.
