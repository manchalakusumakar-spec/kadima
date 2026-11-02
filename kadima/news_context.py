"""Headline topic labels for human review, never sentiment or execution inputs."""
import re


def headline_context(title):
    title = str(title or '').lower()
    matches = []
    rules = [
        (r'\btata sons\b', 'Tata group watchlist',
         'Check the affected listed company and ownership exposure; an adviser view is not an IPO confirmation.'),
        (r'\b(fomc|federal reserve|fed rate|us fed)\b', 'NIFTY, BANKNIFTY, MCX GOLD and SILVER',
         'Compare the decision with expectations, then observe yields, USD/INR and price confirmation.'),
        (r'\b(mpc|repo rate|rbi rate)\b', 'BANKNIFTY and rate-sensitive stocks',
         'Check the actual policy surprise; bank margin and credit-growth effects can offset each other.'),
        (r'\b(crude|opec|oil supply|oil inventories)\b', 'MCX CRUDEOIL and energy-sensitive stocks',
         'Verify supply or inventory news against expectations; watch crude, INR and affected sector prices.'),
        (r'\b(natural gas|gas storage)\b', 'MCX NATURALGAS',
         'Verify the storage/weather surprise and contract month before assessing price reaction.'),
        (r'\b(gold|silver|bullion)\b', 'MCX GOLD and SILVER',
         'Watch real yields, USD/INR and fresh commodity quotes; headline direction may already be priced.'),
    ]
    for pattern, assets, reason in rules:
        if re.search(pattern, title):
            matches.append(dict(watch_assets=assets, reason=reason))
    return dict(basis='headline_topic_only', matches=matches,
                action='Watchlist only — verify source, surprise and market confirmation; no automatic trade.')
