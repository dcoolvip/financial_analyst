#!/bin/zsh
# One time: keep your BofA user ID and password in the macOS Keychain for tools/bofa_fetch.py.
# The password is typed into a hidden prompt here - it never goes into the code, the dashboard or any chat.
# Run again to change it. To remove it: security delete-generic-password -s "FinancialAnalyst BofA"
read "uid?BofA user ID: "
[[ -n "$uid" ]] || { echo "No user ID - nothing saved."; exit 1; }
echo "Now type your BofA password (hidden), then again to confirm:"
security add-generic-password -U -s "FinancialAnalyst BofA" -a "$uid" -l "FinancialAnalyst BofA" -w \
  && echo "Saved in the Keychain as \"FinancialAnalyst BofA\"."
