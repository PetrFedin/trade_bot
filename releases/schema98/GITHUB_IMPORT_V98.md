# Import ASTRA Schema 98 into PetrFedin/trade_bot

The qualified self-contained bundle contains the complete Schema 96 snapshot and the additive Schema 97–98 history. The guarded import preserves the current remote `main` on a timestamped backup branch, verifies the bundle SHA-256 and expected release commit, and updates `main` with an exact `--force-with-lease` guard.

## Expected identity

- final commit: `71a705895a51573b8897c8e48543089366e61c0f`
- final tree: `4faa170db703a3b27caa94060384c13ceda30e22`
- bundle SHA-256: `49216eaf6d88fa498a063dfb4c7124cab4ea6f96f6554ab16cbc25cbf6bb3991`

Download `astra-schema98-self-contained-7.28.0.bundle` and `import-schema98-to-github.sh` from the release delivery into `~/Downloads`, then run:

```bash
cd ~/Projects/trade_bot
chmod +x ~/Downloads/import-schema98-to-github.sh
~/Downloads/import-schema98-to-github.sh \
  ~/Downloads/astra-schema98-self-contained-7.28.0.bundle \
  ~/Projects/trade_bot
```

Expected final output:

```text
Remote main: 71a705895a51573b8897c8e48543089366e61c0f
```

The script refuses a dirty working tree, an unexpected remote, a bundle hash mismatch or a changed remote `main`. A branch-protection rule that forbids force updates will stop the import before replacement.