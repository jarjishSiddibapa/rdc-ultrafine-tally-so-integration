# Contributing

This is a portfolio project, but fixes and suggestions are welcome.

1. Set up the project as described in the [README](README.md#quick-start).
2. Make a branch, keep changes small and focused.
3. Run the tests before opening a pull request:

   ```powershell
   .\venv\Scripts\python.exe -m pytest
   ```

4. Add or update tests for behaviour you change, and a migration (`alembic revision`) for any model change.
5. Never commit `.env`, real customer data or credentials. Use `scripts/seed_demo_database.py` for demo data.
