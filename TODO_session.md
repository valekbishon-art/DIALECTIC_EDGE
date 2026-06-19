# Session TODO (obgaz big request 2026-06-18)

1. [ ] BUG: кнопка 📘 Команды не работает — fix
2. [ ] Remove ALL autotrade/paper-bot user-facing mentions → replace with "API биржи скоро (в разработке)"
3. [ ] Restore pump module reframed as long-momentum spot scanner ("что разгоняется")
4. [ ] Rewrite debate system (Bull/Bear agents → analysis + chart + direction) + prettier chart
5. [ ] Rewrite newbie guide
6. [ ] Add explainer "что это и что делать" to Trend/Stocks/DCA/etc messages
7. [ ] Add deeplinks where missing
8. [ ] Hunt bugs generally

Run tests: env -u VIRTUAL_ENV -u PYTHONHOME -u UV_PROJECT_ENVIRONMENT ./.venv_test/bin/python -m pytest -q --timeout=45
Import: env -u ... ./.venv_test/bin/python -c "import main"
Push: git branch -f spot-only HEAD && git push <AUTH> spot-only:spot-only
