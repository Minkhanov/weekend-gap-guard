"""Use the guard as a library inside an agent loop: decide, then hand the size to the wallet layer.

    python examples/agent_pretrade.py TSLAB 2000
"""
import sys

from gapguard.integrations.agentic_wallet import plan
from gapguard.risk.account import AccountState
from gapguard.risk.engine import OrderIntent, evaluate
from gapguard.risk.policy import GuardPolicy
from gapguard.sources.binance_rwa import RwaClient
from gapguard.views import live_view

symbol, usd = sys.argv[1], float(sys.argv[2])
account = AccountState.load("examples/account.json")
view, book = live_view(RwaClient(), symbol)                 # public endpoints, no key
gaps = {t: m.q99 for t in book.by_ticker if (m := book.gap_model(t, view.at)) and m.q99}
decision = evaluate(OrderIntent(symbol, "buy", usd), view, account, GuardPolicy(), gap_by_ticker=gaps)
print(decision.render())

handoff = plan(decision, view.token.address, view.price)
if handoff.verdict == "BLOCK":
    print("\nno order: " + handoff.reason)
else:
    print("\nshow the user, then run the quote; swap only after explicit confirmation:")
    print(handoff.quote_cmd)
