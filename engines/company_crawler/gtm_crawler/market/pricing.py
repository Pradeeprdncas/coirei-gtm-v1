"""Prices: what an amount pays for, per what and for which plan (rules, no LLM).

Each amount is read with the words of its own clause: the text between two
amounts is split at the last separator (",", ";", "and", "or", "plus", "+",
...) so "$99 setup and $20/month" gives a setup fee and a monthly price. An
amount is a price only when its clause says so (a unit or period after it,
or a price word next to it), or when it stands in a pricing card or a table
cell (``Context.card``); amounts its clause marks as something else (savings,
credits, gift cards, spend thresholds, revenue, funding, investment) are not
prices. What the price pays for (``PricingType``) comes from the words next
to it, its unit from what follows it ("per 1,000 API calls", "/user/month"),
a billing commitment ("billed annually") from the rest of its clause.
Nothing is converted, annualized or assumed beyond what is written: a
missing period stays missing, and a bare "$" is USD only when the page is
not about another dollar's country (``Price.currency_assumed``); otherwise
its currency is None. What a reader should know before using a price is in
``Price.ambiguities``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import (
    Attribution,
    Metric,
    MetricKind,
    Price,
    PriceBound,
    PriceContext,
    PricingType,
)

PRICING_VERSION = "pricing-1"

# Indian digit grouping first ("1,54,000", "10,00,000"), then thousands, then plain.
_NUMBER = r"\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_PREFIX = (r"US\$|U\.S\.\s?\$|USD|S\$|SGD|HK\$|HKD|A\$|AU\$|AUD|C\$|CA\$|CAD|NZ\$|NZD|R\$|BRL|"
           r"MX\$|MXN|Rs\.?|INR|₹|€|EUR|£|GBP|¥|JPY|CNY|RMB|CHF|\$")  # fmt: skip
_CODE = r"USD|EUR|GBP|INR|SGD|HKD|AUD|CAD|NZD|CHF|JPY"
_SUFFIX = r"USD|EUR|GBP|INR|SGD|HKD|AUD|CAD|NZD|CHF|€|£|dollars|euros|rupees"
AMOUNT = re.compile(
    rf"(?<![A-Za-z0-9])(?P<cur>{_PREFIX})\s?(?P<num>{_NUMBER})(?P<k>[kK]\b)?"
    r"(?:\s?(?P<big>trillion|billion|million|crore|lakh|tn|bn|mn|mm|[mb])\b)?"
    rf"(?:\s?(?P<code>{_CODE})(?![A-Za-z]))?"
    rf"|(?<![\w.,])(?P<num2>{_NUMBER})\s?(?P<cur2>{_SUFFIX})(?![A-Za-z])"
    r"(?:\s?(?P<big2>trillion|billion|million|bn|mn)\b)?"
    r"|(?<![\w.,])(?P<cents>\d{1,2})\s?(?:¢|cents?\b)"
    r"|(?<![\w.,])(?P<pence>\d{1,2}(?:\.\d+)?)\s?(?:p(?![.\w])|pence\b)"  # "1.4% + 20p"
    r"|(?<![\w.,])(?P<paise>\d{1,3}(?:\.\d+)?)\s?(?:paise|paisa)\b"
    r"|(?<![\w.,])(?P<pct>\d{1,2}(?:\.\d+)?)\s?%",
    re.I,
)
_CODES = {
    "us$": "USD", "u.s.$": "USD", "usd": "USD", "s$": "SGD", "sgd": "SGD", "hk$": "HKD",
    "hkd": "HKD", "a$": "AUD", "au$": "AUD", "aud": "AUD", "c$": "CAD", "ca$": "CAD",
    "cad": "CAD", "nz$": "NZD", "nzd": "NZD", "r$": "BRL", "brl": "BRL", "mx$": "MXN",
    "mxn": "MXN", "inr": "INR", "₹": "INR", "rupees": "INR",
    "€": "EUR", "eur": "EUR", "euros": "EUR", "£": "GBP", "gbp": "GBP", "jpy": "JPY",
    "cny": "CNY", "rmb": "CNY", "chf": "CHF",
}  # fmt: skip
_BARE = {"$": "USD", "dollars": "USD", "rs": "INR", "rs.": "INR"}  # read from the symbol alone
# Where a bare "$" may be another dollar (or peso).
_DOLLAR_PLACES = re.compile(
    r"\b(singapore|hong kong|australia|australian|canada|canadian|new zealand|taiwan|mexico|"
    r"brazil|chile|argentina|colombia)\b", re.I)  # fmt: skip
_DOLLAR_LOCALE = re.compile(r"(?:^|/)(?:[a-z]{2}[-_])?(sg|hk|au|ca|nz|tw|mx|br|cl|ar)(?:/|$)")
_DOLLAR_TLDS = (".sg", ".hk", ".au", ".ca", ".nz", ".tw", ".mx", ".br", ".cl", ".ar")

_SEP = re.compile(r"[.!?]\s|,|;|\(|\)|\||—|\s–\s|\b(?:and|or|plus|then|but|while|whereas|"
                  r"versus|vs\.?|compared (?:with|to))\b|\+")  # fmt: skip
_RANGE_GAP = re.compile(r"^\s*(?:-|–|—|to|and)\s*$", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")
_LIST_GAP = re.compile(r"^\s*(?:,|,?\s*or|/)\s*$", re.I)
_PAIR_GAP = re.compile(r"^\s*(?:\+|plus|and)\s*$", re.I)  # "2.9% + 30¢"

_PERIODS = {"month": "month", "mo": "month", "mth": "month", "months": "month",
            "year": "year", "yr": "year", "annum": "year", "years": "year", "week": "week",
            "wk": "week", "day": "day", "hour": "hour", "hr": "hour",
            "quarter": "quarter"}  # fmt: skip
_ADVERBS = {"monthly": "month", "annually": "year", "yearly": "year", "weekly": "week",
            "daily": "day", "hourly": "hour", "quarterly": "quarter"}  # fmt: skip
_SEAT = {"user", "seat", "member", "agent", "employee", "editor", "admin", "teammate",
         "team member", "host", "staff", "learner", "student", "technician", "rep", "person",
         "active user", "creator", "collaborator"}  # fmt: skip
_METERED = {"api call", "call", "request", "email", "message", "sms", "text", "minute", "gb",
            "tb", "mb", "credit", "token", "page", "document", "send", "lead", "query", "event",
            "execution", "run", "scan", "lookup", "verification", "envelope", "signature",
            "impression", "click", "view", "kwh", "session", "contact", "subscriber",
            "recipient", "active contact", "profile", "mau"}  # fmt: skip
_TRANSACTION = {"transaction", "order", "payment", "sale", "charge", "swipe", "booking",
                "transfer", "payout", "tap", "card transaction"}  # fmt: skip
_GOODS = {"lb", "kg", "oz", "g", "item", "unit", "piece", "pack", "bottle", "serving", "meal",
          "box", "case", "bag", "can", "dozen", "litre", "gallon", "ml", "portion", "patty",
          "burger", "sq ft", "pair"}  # fmt: skip
_OTHER = {"location", "site", "website", "domain", "store", "shop", "account", "workspace",
          "team", "project", "device", "vehicle", "license", "server", "node", "instance",
          "property", "brand", "channel", "integration", "number", "phone number", "line",
          "mailbox", "inbox", "charger", "port", "connector", "board", "app", "module",
          "invoice", "client", "report", "course", "class", "household"}  # fmt: skip
_SYNONYMS = {"pound": "lb", "lbs": "lb", "kilogram": "kg", "kilo": "kg", "ounce": "oz",
             "gram": "g", "licence": "license", "liter": "litre", "api": "api call",
             "api calls": "api call", "texts": "text", "square foot": "sq ft",
             "people": "person", "credit card transaction": "transaction",
             "debit card transaction": "transaction", "credit card": "transaction",
             "card": "transaction", "card transaction": "transaction"}  # fmt: skip
_BASES = _SEAT | _METERED | _TRANSACTION | _GOODS | _OTHER
_NOUN = (
    r"[a-z][a-z.]*(?:\s(?:card|calls?|requests?|contacts?|users?|members?|numbers?|"
    r"transactions?|foot|ft)){0,2}"
)
_STEP = re.compile(
    r"^\s*(?:(?:/|\bper\b|\ba\b|\ban\b|\beach\b|\bevery\b|\bfor each\b|\bfor every\b)\s*"
    rf"(?:(?P<q>\d[\d,.]*\s?k?)\s*)?(?P<noun>{_NOUN})"
    r"|(?P<adv>monthly|annually|yearly|weekly|daily|hourly|quarterly)\b)",
    re.I,
)

_NOT_BEFORE = re.compile(
    r"\b(?:save[sd]?(?: (?:you|users|customers|teams|businesses))?|saving|savings of|"
    r"refunds?(?: of)?|refunded|cash ?back(?: of)?|"
    r"rebates?(?: of)?|"
    r"discount(?:ed)? (?:of|by)|rewards?(?: of)?|bonus(?: of)?|credits? (?:of|worth)|"
    r"gift cards?(?: worth| of)?|vouchers?(?: worth| of)?|coupons? (?:for|worth|of)|"
    r"spend(?:s|ing)?(?: at least| over| more than| up to| above| of| under| below|"
    r" less than| an average of| on average| about| around)?|spent|average of|"
    r"generat(?:e|es|ed|ing)|makes?|made|bring(?:s|ing)? in|brought in|"
    r"orders? (?:over|above|of)(?: at least)?|purchases? (?:over|above|of)|"
    r"minimum (?:order|spend|purchase)(?: value)?(?: of)?|"
    r"free (?:shipping|delivery)(?: on (?:orders|purchases))? (?:over|above|from)|"
    r"revenues?(?: of)?|sales of|turnover of|earn(?:s|ed|ing)?|income of|salary(?: of)?|"
    r"wages? of|raised|funding(?: of)?|valued at|valuation of|worth|invest\w*(?: of)?|"
    r"capex(?: of)?|capital expenditure(?: of)?|budget(?: of)?|grants? of|subsid\w*(?: of)?|"
    r"fines?(?: of| up to)?|penalt\w+(?: of| up to)?|deal size(?: is| of)?|"
    r"order value(?: is| of)?|aov(?: is| of)?|ltv(?: is| of)?|arpu(?: is| of)?|generated|"
    r"paid out|payouts? of|average (?:customer|user|order|deal|annual|household)\w*"
    r"(?:\s\w+){0,3}?\s(?:is|of|was))"
    r"\s*(?:(?:about|around|nearly|almost|approximately|an? (?:extra|additional)|up to|over|"
    r"more than|at least|just|only|roughly)\s)?$",
    re.I,
)
_NOT_AFTER = re.compile(
    r"^\s*(?:off\b|discount|in (?:savings|credits?|free credits?|revenue|sales|funding|rewards|"
    r"cash ?back|value)|(?:of|worth of|in)\s(?:free\s)?(?:\w+\s){0,2}?(?:credits?|vouchers?|"
    r"rewards?)\b|(?:free\s|ad\s|cloud\s|platform\s|account\s|promotional\s|startup\s)?"
    r"credits?\b|gift ?cards?|vouchers?|coupons?|cash ?back|rebates?|savings?\b|"
    r"(?:in |of )?(?:annual |monthly |recurring |paid |gross |net |total )?"
    r"(?:revenue|arr|mrr|turnover|funding|gmv)\b|"
    r"or more (?:in|on) (?:orders|purchases))",
    re.I,
)
_ALLOWANCE = re.compile(r"^\s*(?:of|worth of)\s(?!each\b|every\b|the\b|your\b)[a-z]", re.I)
_NON_PRICE_NOUN = re.compile(
    r"\b(?:subsid(?:y|ies|ised|ized)|grants?|investments?|invested|funding|capex|"
    r"capital expenditures?|revenues?|turnover|salar(?:y|ies)|wages?|earnings|income)\b"
    r"(?P<rest>(?:(?![.!?]\s)[^$€£₹¥])*)$",
    re.I)  # fmt: skip
_NOT_PRICE_LABEL = re.compile(
    r"\b(?:referral|subsid\w*|grants?|discounts?|savings?|you save|refunds?|rebates?|"
    r"cash ?back|revenue|bonus|rewards?|commission earned|payouts? to you)\b", re.I)  # fmt: skip
_SETUP = re.compile(
    r"\b(?:set[- ]?up|implementation|onboarding|installation|install|activation|migration fee|"
    r"start-?up fee|joining fee|enrol?ment fee|registration fee)\b", re.I)  # fmt: skip
_ADD_ON = re.compile(r"\b(?:add[- ]?ons?|bolt[- ]ons?|optional extras?|(?:additional|extra) "
                     r"(?:team )?(?:seats?|users?|members?))\b", re.I)  # fmt: skip
_MINIMUM = re.compile(r"\bminimum (?:monthly |annual |yearly )?(?:fee|charge|billing|payment|"
                      r"commitment)\b", re.I)  # fmt: skip
_TRANSACTION_CUE = re.compile(
    r"\b(?:(?:transaction|processing|payment|card|checkout|interchange) (?:fees?|rates?|costs?)|"
    r"commission|per (?:sale|order|transaction|booking|payment|swipe|tap)|take rate|"
    r"of (?:each|every|the|your) (?:sale|order|transaction|payment|booking|invoice)|"
    r"(?:card|online|in-person|ach|bank) (?:payments?|transactions?))\b", re.I)  # fmt: skip
_PERCENT_FEE = re.compile(r"\b(?:fees?|charges?d?|commission|transaction|processing|payment|"
                          r"card|per (?:sale|order|booking)|take rate)\b", re.I)  # fmt: skip
_USAGE = re.compile(r"\b(?:overages?|pay[- ]as[- ]you[- ]go|usage[- ]based|metered|per use|"
                    r"additional (?:usage|credits?|emails?|messages?|contacts?|minutes?))\b",
                    re.I)  # fmt: skip
_ONE_TIME = re.compile(r"\b(?:one[- ]time|one[- ]off|lifetime|pay once|single payment|"
                       r"perpetual|outright|buy it|purchase price)\b", re.I)  # fmt: skip
_SUBSCRIPTION = re.compile(r"\b(?:plans?|subscriptions?|subscribe|memberships?|tiers?|"
                           r"packages?|editions?|licen[cs]es?|renews?)\b", re.I)  # fmt: skip
_FEE_KIND = re.compile(rf"{_SETUP.pattern}|{_ADD_ON.pattern}|{_ONE_TIME.pattern}", re.I)
_PRICE_WORD = re.compile(
    r"\b(?:pric(?:e|es|ed|ing)|costs?|costing|fees?|charges?d?|pay|pays|paid|billed|buy|"
    r"purchase|sells? for|retails?|msrp|rrp|for (?:just|only)|starts?|starting|as low as|"
    r"subscriptions?|plans?|per|rates? of|fares?|tickets?|one[- ]way|round[- ]trip|return fare|"
    r"pp)\b"
    r"|/\s?(?:mo|month|yr|year)\b", re.I)  # fmt: skip
# Next to the amount: a was-price ("£29.50 was £36.00"), a ticket's way ("£78 return").
_PRICE_BESIDE = re.compile(r"\b(?:previously|originally|regular price|list price)\s*:?\s*$",
                           re.I)  # fmt: skip
_PRICE_AFTER = re.compile(r"^\s*(?:was\b|one[- ]way\b|return\b|each way\b|pp\b|per person\b)",
                          re.I)  # fmt: skip
_WAS_GAP = re.compile(r"\s*was\s*:?\s*", re.I)  # between a sale and a was-price
_EXTRA = re.compile(r"\b(?:an?\s)?extra\s*$|\bsupplements?\b", re.I)  # "an extra $10"
# A pack of metered units: "1250 INR + GST for 5,000 SMS Units", "1,000 Messages – 295 INR".
_PACK_NOUN = r"(?:units?|messages?|sms|credits?|emails?|minutes?|calls?|texts?)"
_PACK_AFTER = re.compile(rf"^\s*(?:\+\s*(?:gst|vat|tax(?:es)?)\s*)?for\s\d[\d,.]*\s?k?\s"
                         rf"(?:[a-z]+\s)?{_PACK_NOUN}\b", re.I)  # fmt: skip
_PERIODIC = re.compile(r"\bper (?:month|year)\b|/\s?(?:mo|month|yr|year)\b|\b(?:monthly|annually|"
                       r"yearly|a month|a year)\b", re.I)  # fmt: skip
_PACK_BEFORE = re.compile(rf"\b\d[\d,.]*\s?k?\s(?:[a-z]+\s)?{_PACK_NOUN}\s*[–—:-]\s*$", re.I)
_PROMO = re.compile(
    r"\b(?:(?:first|1st)\s(?:\d+|one|two|three|four|six|twelve)\s(?:months?|years?|weeks?)|"
    r"for (?:the )?first|intro(?:ductory)?|limited[- ]time|promo(?:tion(?:al)?)?|"
    r"special (?:offer|price|pricing)|launch (?:price|offer|pricing|discount)|discounted|"
    r"sale price|black friday|cyber monday|early[- ]bird)\b|\b\d+% off\b", re.I)  # fmt: skip
_NOW = re.compile(r"\bnow\s*(?:only\s|just\s)?$", re.I)
_FROM = re.compile(r"(?:\bstarts?(?:\s(?:at|from))?|\bstarting(?:\s(?:at|from|price))?|"
                   r"\bbeginning at|\bbegins at|\bfrom|\bas low as|\bas little as)\s*:?\s*"
                   r"(?:just\s|only\s)?$", re.I)  # fmt: skip
_UP_TO = re.compile(r"\b(?:up to|upto|maximum(?: of)?|max\.?|capped at|no more than|"
                    r"not exceeding|at most)\s*$", re.I)  # fmt: skip
_ABOUT = re.compile(r"(?:\b(?:about|around|approximately|approx\.?|roughly|circa|nearly|"
                    r"almost)|~|≈)\s*$", re.I)  # fmt: skip
_BILLED = re.compile(
    r"\b(?:billed|paid|payable|charged)\s(?:as\s)?(?:one\s)?(?P<a>annually|yearly|annual|monthly|"
    r"quarterly|every (?:month|year|quarter)|upfront)"
    r"|\b(?P<c>annual|yearly|monthly|quarterly)\s(?:billing|commitment|contract|prepay\w*|"
    r"subscription|plan)s?\b|\b(?P<m>month[- ]to[- ]month)\b"
    r"|\bon (?:an?|the) (?P<d>annual|yearly|monthly) (?:plan|basis|billing)", re.I)  # fmt: skip
_NOT_BILLED = re.compile(r"\b(?:save|discount|no|without|off)\b[^.$€£]{0,30}$", re.I)
# "per month: $29" is a period; "Monthly: $29", "annual plan $290" are billing (below).
_PERIOD_BEFORE = re.compile(r"\b(?P<w>per month|a month|per year|a year|per week|per day)\b"
                            r"[^$€£₹\d]{0,25}$", re.I)  # fmt: skip
_BILLED_WORD = re.compile(r"\b(?P<w>monthly|annual(?:ly)?|yearly|quarterly)\b", re.I)
_BASIS_BEFORE = re.compile(r"\bper[- ](?P<n>user|seat|member|agent|employee)\b[^$€£₹\d]{0,25}$",
                           re.I)  # fmt: skip
_PLAN_NAMED = re.compile(r"\b(?P<p>[A-Z][\w+&'-]*(?:\s[A-Z0-9][\w+&'-]*){0,2})\s(?:plan|tier|"
                         r"package|edition|bundle|subscription|membership)s?\b")  # fmt: skip
_PLAN_LABEL = re.compile(r"^\W*(?P<p>(?:[A-Z][\w+&'-]*|[a-z][\w-]*\.[a-z]{2,})"
                         r"(?:\s[A-Z0-9][\w+&'-]*){0,2})\s?(?:[:–—-]|\(|\bis\b|\bcosts?\b|"
                         r"\bat\b|\bfor\b|\bstarts? at\b|\bruns\b|\bcharges\b|"
                         r"\bis priced at\b|)\s*(?:only\s|just\s)?$")  # fmt: skip
# "Mailchimp: At the 5,001-10,000 tier ...": a comparison's sentence led by a name.
_LEAD = re.compile(r"^\W*(?P<p>[A-Z][\w.+&'-]*(?:\s[A-Z0-9][\w.+&'-]*){0,2}):\s")
_NOT_PLAN = frozenset(
    "the our this that each every any all your their its a an monthly annual yearly paid "
    "pricing price prices same which one per with new current most popular recommended it "
    "there plans cost costs total we they you starting starts from billed save get try "
    "free trial custom standard? and or but if excl excl. incl incl. vat gst tax save "
    "billed paid only just now was is at to for under over up about around between "
    "by on in as of less more than".split()
)
_ADD_ON_NAME = re.compile(r"\b(?:the\s)?(?P<p>[A-Z][\w-]*(?:\s[A-Z][\w-]*){0,3})\s"
                          r"(?:add[- ]?on|bolt[- ]on)\b")  # fmt: skip
_ALLOWANCE_NOUN = (r"(?:monthly\s|active\s|marketing\s)?(?:contacts?|emails?(?:\ssends?)?|sends|"
                   r"users?|seats?|invoices?|clients?|subscribers?|messages?|sms|texts?|gb|tb|"
                   r"projects?|sites?|websites?|products?|orders?|transactions?|minutes?|"
                   r"credits?|requests?|api calls?|calls?|documents?|members?|team members?|"
                   r"locations?|employees?|mailboxes?|profiles?|leads?|recipients?)"
                   r"(?:\s?(?:/|per|a)\s?(?:month|mo|year))?")  # fmt: skip
_INCLUDED = re.compile(
    r"\b(?:for|includes?|including|included|with|covers?)\s(?:up to\s|the first\s)?"
    rf"(?P<a>\d[\d,.]*\s?[kKmM]?\+?\s{_ALLOWANCE_NOUN})\b"
    rf"|(?P<b>\d[\d,.]*\s?[kK]?\+?\s{_ALLOWANCE_NOUN})\s(?:included|per month included)",
    re.I,
)
_TAX_EX = re.compile(r"\b(?:excl(?:\.|uding|usive of)?|ex\.?|plus|before|not including)\s?"
                     r"(?:vat|gst|hst|sales tax|taxes|tax)\b|\+\s?(?:vat|gst|tax)\b|"
                     r"\b(?:vat|gst|tax(?:es)?) (?:not included|excluded|extra)\b",
                     re.I)  # fmt: skip
_TAX_IN = re.compile(r"\b(?:incl(?:\.|uding|usive of)?|inc\.?)\s?(?:vat|gst|hst|sales tax|"
                     r"taxes|tax)\b|\b(?:vat|gst|tax(?:es)?) included\b|\btax[- ]inclusive\b",
                     re.I)  # fmt: skip
_QUOTE = re.compile(
    r"\b(?:contact (?:sales|us)(?: for (?:pricing|a quote|details|a custom \w+))?|"
    r"custom (?:pricing|quote|plans?)|talk to (?:sales|us)|get a (?:custom )?quote|"
    r"request (?:a )?(?:quote|pricing|demo for pricing)|pric(?:e|ing) (?:on|upon) request|"
    r"call for pricing|let's talk)\b", re.I)  # fmt: skip
_QUOTE_CONTEXT = re.compile(r"\b(?:pric(?:e|es|ing)|quote|plans?|enterprise|custom)\b", re.I)
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
EFFECTIVE = re.compile(
    r"\b(?:(?:prices?|pricing|rates?|fees?|plans?)\b[^.]{0,40}?\b(?:effective|as of|valid from|"
    r"valid until|updated|current as of)|last updated|updated on|verified|prices as of)\s*:?\s*"
    rf"(?:on\s)?(?P<d>(?:{_MONTHS}\s(?:\d{{1,2}},?\s)?|\d{{1,2}}\s{_MONTHS}\s)?(?:19|20)\d\d)",
    re.I,
)
PRICING_PAGE = re.compile(r"\b(?:pricing|prices|plans)\b", re.I)
# A page comparing several vendors' prices is not one vendor's pricing page.
_ROUNDUP = re.compile(r"\b(?:compar\w*|vs\.?|versus|alternatives?|best|reviews?|blog|"
                      r"top \d+|roundup)\b", re.I)  # fmt: skip
# "(Starter)" after an amount names its plan.
_PAREN_PLAN = re.compile(r"^\s*\((?P<p>[A-Z][\w+&'-]*(?:\s[A-Z0-9][\w+&'-]*){0,2})\)")
# "Kit's 3.5% + 30c", "Klaviyo's entry plan is $20/mo": whose price it is.
_OWNER = re.compile(r"(?<![\w.])(?P<p>[A-Z][\w.-]*(?:\s[A-Z][\w.-]*){0,2})['’]s\s"
                    r"(?:[^$€£₹¥'’]{0,40})$")  # fmt: skip


@dataclass(frozen=True)
class Context:
    """What is known about where a text sits."""

    vendor: str | None = None  # the known competitor whose page it is
    plan: str | None = None  # from the heading above, or a table's column or row
    product: str | None = None  # a heading on a page that is not the vendor's own
    labels: str = ""  # a table cell's row and column labels: read for terms
    card: bool = False  # a pricing-card element or a table cell: an amount is a price
    dollar: str | None = None  # a place where "$" may be another dollar
    effective: str | None = None  # the page's "prices as of"
    context: tuple[PriceContext, ...] = ()
    own: int | None = None  # the excerpt's length; text after it is the next elements'
    third_party: bool = False  # neither the vendor's own nor a pricing page: names are products
    # The page's own brand (vendor, site name, domain label), normalized: "Staffly Basic"
    # on staffly.sg is the Basic plan; a "product" that is the brand is the vendor.
    brands: frozenset[str] = frozenset()
    plan_from: Attribution | None = None  # where ``plan`` / ``product`` above came from


def dollar_place(url: str | None, *texts: str | None) -> str | None:
    """A place a page is about where a bare "$" may not be US dollars."""
    host = urlsplit(url).hostname or "" if url else ""
    for tld in _DOLLAR_TLDS:
        if host.endswith(tld):
            return tld
    path = urlsplit(url).path.lower() if url else ""
    if m := _DOLLAR_LOCALE.search(path):
        return f".{m.group(1)}"
    for text in texts:
        if text and (m := _DOLLAR_PLACES.search(text)):
            return m.group(1).title()
    return None


def has_amount(text: str) -> bool:
    """Whether ``text`` states an amount of money (not a percentage)."""
    return any(not m.group("pct") for m in AMOUNT.finditer(text))


def quote_based(text: str) -> bool:
    """Whether ``text`` says the price is given on request ("Contact sales")."""
    return bool(_QUOTE.search(text))


_PRICE_LABEL = re.compile(r"\b(?:pric(?:e|es|ing)|costs?|fees?|amount|monthly|annual(?:ly)?|"
                          r"yearly|hourly|daily|weekly|quarterly|per|total|rates?|charges?|usd|"
                          r"eur|gbp)\b", re.I)  # fmt: skip


def add_on_heading(text: str) -> bool:
    return len(text) <= 40 and bool(_ADD_ON.search(text))


def price_label(text: str) -> bool:
    """Whether a table label names a kind of price ("Monthly price"), not a plan."""
    return bool(_PRICE_LABEL.search(text))


_NOT_BASIS = frozenset(
    "the cent cents annum your our their each every year's usual default instance".split()
)


def _pack(text: str) -> tuple[str | None, float | None]:
    """(basis, quantity) of a pack ("for 5,000 SMS Units" -> sms, 5000)."""
    num = re.search(r"\d[\d,.]*", text)
    nouns = re.findall(rf"[a-z]+(?=\s*{_PACK_NOUN}\b)|{_PACK_NOUN}\b", text, re.I)
    noun = next((n for n in nouns if _basis(n) and n.lower() not in ("unit", "units")),
                nouns[-1] if nouns else "")  # fmt: skip
    k = 1000 if re.search(r"\d\s?k\b", text, re.I) else 1
    return _basis(noun), float(num.group(0).replace(",", "")) * k if num else None


def _basis(noun: str) -> str | None:
    n = noun.lower().strip(". ")
    n = _SYNONYMS.get(n, n)
    for cand in (n, n[:-1] if n.endswith("s") else n, n[:-2] if n.endswith("es") else n):
        cand = _SYNONYMS.get(cand, cand)
        if cand in _BASES or cand in _PERIODS:
            return cand
    return None


def _units(after: str) -> tuple[str | None, str | None, float | None, int]:
    """(period, basis, basis quantity, characters read) of "/user/month", "per 1,000 calls"."""
    period = basis = None
    quantity: float | None = None
    pos = 0
    for _ in range(3):
        m = _STEP.match(after[pos:])
        if m is None:
            break
        if m.group("adv"):
            if period is not None:
                break
            period = _ADVERBS[m.group("adv").lower()]
        else:
            word = _basis(m.group("noun"))
            # "a" / "an" only before a period or a count noun ("a month", "an hour").
            lead = m.group(0).strip().split()[0].lower()
            noun = m.group("noun").lower().strip(". ")
            if word is None and lead == "per" and re.fullmatch(r"[a-z]{3,15}", noun) \
                    and noun not in _NOT_BASIS:  # fmt: skip
                word = noun[:-1] if noun.endswith("s") and not noun.endswith("ss") else noun
            if word is None or (lead in ("a", "an") and word not in _PERIODS and word not in
                                _SEAT):  # fmt: skip
                break
            if word in _PERIODS:
                q = (m.group("q") or "").replace(",", "").strip()
                if period is not None or q and q not in ("1", "1.0"):
                    break  # "$120 per 12 months": no period is assumed
                period = _PERIODS[word]
            else:
                if basis is not None:
                    break
                basis = word
                if q := m.group("q"):
                    q = q.lower().replace(",", "").replace(" ", "")
                    mult = 1000.0 if q.endswith("k") else 1.0
                    try:
                        value = float(q.rstrip("k")) * mult
                    except ValueError:
                        value = 1.0
                    quantity = value if value != 1 else None
        pos += m.end()
    return period, basis, quantity, pos


_CONJUNCTION = re.compile(r"^\W*(?:and|or|but|while|whereas|then|plus)\s+", re.I)


def _plan(text: str, vendor: str | None) -> str | None:
    for m in reversed(list(_PLAN_NAMED.finditer(text))):
        # "Billtastic's Growth plan": the plan is what follows the owner.
        words = re.split(r"\S+['’]s\s", m.group("p"))[-1].split()
        # "The Free plan" names the Free plan; "Annual plan", "Paid plan" name none.
        while words and words[0].lower() in _NOT_PLAN and words != ["Free"]:
            words.pop(0)
        if words and " ".join(words).lower() != (vendor or "").lower():
            return " ".join(words)
    return None


def _label(before: str, vendor: str | None) -> str | None:
    """The name a clause gives its amount: "Pro: $29", "Brevo $8/mo", the part of a
    sentence after its last colon ("... sit close together: Brevo $8/mo")."""
    before = _CONJUNCTION.sub("", before)  # "and Staffly Plus is S$14"
    m = _PLAN_LABEL.match(before) or _PLAN_LABEL.match(
        _CONJUNCTION.sub("", re.split(r"[:;,]\s", before)[-1]))  # fmt: skip
    if m is None:
        return None
    words = m.group("p").split()
    if words[0].lower() in _NOT_PLAN or m.group("p").lower() == (vendor or "").lower():
        return None
    return m.group("p")


def _apart(name: str, plan: str, vendor: str | None) -> bool:
    """A name that is neither the plan ("Ledgerly Pro" for "Pro") nor the vendor's."""
    n, p = name.lower(), plan.lower()
    return p not in n and n not in p and not (vendor and vendor.lower() in n)


def _lead(text: str, vendor: str | None) -> str | None:
    m = _LEAD.match(text)
    if m is None or m.group("p").split()[0].lower() in _NOT_PLAN:
        return None
    return m.group("p") if m.group("p").lower() != (vendor or "").lower() else None


def _owner(before: str) -> str | None:
    m = _OWNER.search(before[-60:])
    return m.group("p") if m and m.group("p").split()[0].lower() not in _NOT_PLAN else None


# Plan and tier words: "PingWatch Pro" on a comparison page is PingWatch's Pro plan.
TIERS = frozenset(
    "free starter basic standard plus pro premium business enterprise team teams growth "
    "essentials essential professional advanced ultimate unlimited personal lite light "
    "developer scale startup solo agency individual family core max elite hobby launch "
    "premier deluxe".split()
)
# "Vine tomatoes - $3.99/kg", "A web licence is €49 per year", "International cards: an
# additional 1.5%": the clause's subject names what is priced.
_SUBJECT = re.compile(
    r"^\W*(?:(?:a|an|the|each|every)\s)?(?P<p>[A-Za-z][\w'’&/.-]*(?:\s[A-Za-z0-9][\w'’&/.-]*)"
    r"{0,4}?)\s*(?:[:–—-]|\bis\b|\bare\b|\bcosts?\b|\bruns\b|\bstarts? at\b)\s*"
    r"(?:only\s|just\s|(?:an\s)?(?:additional|extra)\s)?$", re.I)  # fmt: skip
# A research publisher's price for its own report ("Single User License: $4,650", "The
# full report costs $2,950"): a price of the report, never of the market's products.
_REPORT = re.compile(
    r"\b(?:(?:single|multi|enterprise|corporate|global|team|site|group)[- ]?(?:user|site|seat)?\s?"
    r"licen[cs]e|(?:full|this|the|research|market) report(?:['’]s)?\s(?:costs?|price|pricing|"
    r"is priced|is available)|buy (?:this|the|full) report|report (?:price|pricing|"
    r"licen[cs]e))\b", re.I)  # fmt: skip
REPORT_PRICE = "the price of a research report itself, not of the market's products"
# A review's "At $129, the X is ...": the sentence opens on the amount.
_AT_PRICE = re.compile(r"(?:^|[.!?]\s)\W*at\s*$", re.I)
_NOT_SUBJECT = frozenset(
    "it its this that these those they their we our you your which what who everything "
    "anything nothing all need needs get gets plan plans additional extra up while whereas "
    "but and or so then if when".split()
)


def brand_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def debrand(name: str | None, brands: frozenset[str]) -> str | None:
    """``name`` without a leading brand ("Staffly Basic" -> "Basic"); None when it is
    only the brand (the vendor itself is neither a plan nor a product)."""
    if not name or not brands:
        return name
    words = name.replace("’", "'").split()
    for k in range(min(3, len(words)), 0, -1):
        if brand_key(re.sub(r"'s$", "", " ".join(words[:k]))) in brands:
            return " ".join(words[k:]) or None
    return name


def _split_tier(name: str) -> tuple[str, str] | None:
    """("PingWatch", "Pro") for "PingWatch Pro" or "PingWatch's Pro": a product name
    ending in a tier word."""
    if m := re.fullmatch(r"(?P<a>[A-Z][\w.-]*(?:\s[A-Z][\w.-]*){0,2})['’]s\s(?P<b>.+)", name):
        return m.group("a"), m.group("b")
    words = name.split()
    if len(words) > 1 and words[-1].lower() in TIERS and words[0][:1].isupper():
        return " ".join(words[:-1]), words[-1]
    return None


def _subject(before: str, listed: bool = False) -> str | None:
    """``listed``: an item of an "Add-ons" list, whose label ("Extra phone number") is
    its name even when it starts "extra" or "additional"."""
    seg = _CONJUNCTION.sub("", re.split(r"[.!?]\s|[;,]\s|\(", before)[-1])
    m = _SUBJECT.match(seg)
    if m is None:
        return None
    p = re.split(r"\s(?:for|with|in|on|at|per|of|from)\s", m.group("p"), maxsplit=1)[0]
    words = {re.split(r"['’]", w.lower().strip(".'’"))[0] for w in p.split()}
    if listed:
        words -= {"extra", "additional"}
    if p[:1].isdigit() or words & (_NOT_SUBJECT | _NOT_PLAN) or _PRICE_LABEL.search(p) \
            or _SETUP.search(p) or _MINIMUM.search(p):  # fmt: skip
        return None
    return p


def _currency(m: re.Match[str], ctx: Context) -> tuple[str | None, bool, str | None]:
    """(currency, assumed, ambiguity)."""
    if m.group("pct"):
        return None, False, None
    if m.group("pence"):
        return "GBP", False, None
    if m.group("paise"):
        return "INR", False, None
    sym = "$" if m.group("cents") else \
        (m.group("code") or m.group("cur") or m.group("cur2") or "").lower()  # fmt: skip
    if sym in _CODES:
        return _CODES[sym], False, None
    if sym in ("$", "dollars"):
        if ctx.dollar:
            return None, False, f"currency unclear: '$' on a page about {ctx.dollar}"
        return "USD", True, "currency read from a bare '$' as USD"
    if sym == "¥":
        return None, False, "currency unclear: '¥' is yen or yuan"
    if sym in _BARE:
        return _BARE[sym], True, f"currency read from a bare '{sym}'"
    return None, False, "currency not recognized"


@dataclass
class _Read:
    m: re.Match[str]
    before: str
    after: str
    value: float = 0.0
    percent: bool = False
    period: str | None = None
    basis: str | None = None
    quantity: float | None = None
    consumed: int = 0


def _gaps(text: str, ms: list[re.Match[str]]) -> list[tuple[str, str]]:
    """(before, after) of each amount: the text between two amounts is split at its last
    separator; the first amount looks back and the last looks ahead a little."""
    cuts: list[int] = []
    for a, b in zip(ms, ms[1:], strict=False):
        gap = text[a.end() : b.start()]
        seps = list(_SEP.finditer(gap))
        cuts.append(a.end() + (seps[-1].start() if seps else _units(gap)[3]))
    out = []
    for i, m in enumerate(ms):
        lo = cuts[i - 1] if i else max(0, m.start() - 120)
        hi = cuts[i] if i < len(cuts) else min(len(text), m.end() + 140)
        ends = [e.end() for e in _SENTENCE_END.finditer(text, lo, m.start())]
        lo = ends[-1] if ends else lo
        nxt = _SENTENCE_END.search(text, m.end(), hi)
        hi = nxt.start() + 1 if nxt else hi
        out.append((text[lo : m.start()], text[m.end() : hi]))
    return out


def prices(text: str, ctx: Context | None = None) -> list[Metric]:
    """The prices ``text`` states (``Metric`` with ``price`` terms), in order; a pricing
    statement without an amount ("Contact sales for pricing") is a PRICE_ON_REQUEST."""
    ctx = ctx or Context()
    ms = list(AMOUNT.finditer(text))
    own = ctx.own if ctx.own is not None else len(text)
    if not any(m.start() < own and not m.group("pct") for m in ms):
        return _on_request(text, ctx, own)
    reads: list[_Read] = []
    for m, (before, after) in zip(ms, _gaps(text, ms), strict=True):
        r = _Read(m, before, after)
        num = m.group("num") or m.group("num2") or m.group("cents") or m.group("pence") \
            or m.group("paise") or m.group("pct")  # fmt: skip
        r.value = float(num.replace(",", ""))
        if m.group("cents") or m.group("pence") or m.group("paise"):
            r.value /= 100
        if m.group("k"):
            r.value *= 1000
        r.percent = bool(m.group("pct"))
        r.period, r.basis, r.quantity, r.consumed = _units(after)
        reads.append(r)
    # A range or a fee pair shares the unit written once after it: "$10-$50 per month",
    # "2.9% + 30¢ per transaction".
    linked = [False] * len(reads)
    for i in range(len(reads) - 2, -1, -1):
        a, b = reads[i], reads[i + 1]
        gap = text[a.m.end() : b.m.start()]
        shared = _RANGE_GAP.match(gap) or (a.percent and not b.percent and _PAIR_GAP.match(gap)) \
            or (_LIST_GAP.match(gap) and a.percent == b.percent)  # fmt: skip
        linked[i + 1] = bool(shared)
        if shared and not (a.period or a.basis):
            a.period, a.basis, a.quantity = b.period, b.basis, b.quantity
            a.after = a.after + b.after
    out: list[Metric] = []
    last_end = 0
    rejected = False
    for i, r in enumerate(reads):
        if not (linked[i] and rejected):
            rejected = not r.percent and _non_price(r.before)
        if r.m.start() >= own or r.m.group("big") or r.m.group("big2") or rejected:
            continue
        if not r.percent and r.value >= 1e6:
            continue  # a market figure (market/extract.py), not a price
        metric = _price(text, r, reads, i, ctx, own)
        if metric is None:
            continue
        p, last = metric.price, out[-1].price if out else None
        # "The Team plan is $12 per user per month, €11 in Europe, or £100 per year": an
        # amount whose clause names nothing belongs to the plan its sentence named.
        if p is not None and last is not None and p.attribution is None and \
                last.attribution is Attribution.CLAUSE and not _PLANNISH.search(r.before) \
                and not _SENTENCE_END.search(text[last_end : r.m.start()]):  # fmt: skip
            p.plan, p.product, p.attribution = last.plan, last.product, Attribution.CLAUSE
        out.append(metric)
        last_end = r.m.end()
    return out


_PLANNISH = re.compile(r"\b(?:plans?|tiers?|packages?|editions?)\b", re.I)


def _non_price(before: str) -> bool:
    """A subsidy, an investment, revenue: the clause before the amount names what it is,
    with no price word between ("subsidies ... amount to between EUR 7,000")."""
    m = _NON_PRICE_NOUN.search(before[-90:])
    return m is not None and not _PRICE_WORD.search(m.group("rest"))


def _price(text: str, r: _Read, reads: list[_Read], i: int, ctx: Context,
           own: int) -> Metric | None:  # fmt: skip
    before, after = r.before, r.after
    near = f"{before[-45:]} | {after[:50]} | {ctx.labels}"
    if _NOT_BEFORE.search(before) or _NOT_AFTER.match(after[r.consumed :] if r.consumed else
                                                       after):  # fmt: skip
        return None
    if not r.percent and _ALLOWANCE.match(after):
        return None
    if ctx.labels and _NOT_PRICE_LABEL.search(ctx.labels):
        return None  # "Referral credit / month", "Employer subsidy"
    paired = (i + 1 < len(reads) and r.percent and not reads[i + 1].percent
              and _PAIR_GAP.match(text[r.m.end() : reads[i + 1].m.start()])) or (
              i > 0 and not r.percent and reads[i - 1].percent
              and _PAIR_GAP.match(text[reads[i - 1].m.end() : r.m.start()]))  # fmt: skip
    if r.percent:
        if not (paired or _PERCENT_FEE.search(near)) or re.match(r"^\s*off\b", after, re.I):
            return None
    elif not (
        r.period
        or r.basis
        or ctx.card
        or _PRICE_WORD.search(near)
        or paired
        or _FEE_KIND.search(near)
        or _REPORT.search(near)  # "Single User License: $4,650"
        or _AT_PRICE.search(before)  # "At $129 the Nova wallet is our pick"
        or _FROM.search(before)  # "Adult From £51"
        or _PRICE_BESIDE.search(before)
        or _PRICE_AFTER.match(text[r.m.end() :])
        or i > 0
        and _WAS_GAP.fullmatch(text[reads[i - 1].m.end() : r.m.start()])
        or _EXTRA.search(before[-40:])
        or _PACK_AFTER.match(after)
        or _PACK_BEFORE.search(before)
        or _TAX_EX.match(after.lstrip(" ,("))
        or _TAX_IN.match(after.lstrip(" ,("))  # "+ GST"
    ):  # "the add-on for $49", "$99 setup and ..."
        return None
    ambiguities: list[str] = []
    currency, assumed, why = _currency(r.m, ctx)
    if why:
        ambiguities.append(why)
    period, basis, quantity = r.period, r.basis, r.quantity
    labels = ctx.labels
    # A label's "/ month" or "per user" is a unit; its "Monthly" is billing (below).
    if labels and not (period or basis) and (at := re.search(r"/|\bper\b", labels, re.I)):
        period, basis, quantity, _ = _units(labels[at.start() :])
    if period is None and (pb := _PERIOD_BEFORE.search(before)):
        w = pb.group("w").lower()
        period = "month" if "month" in w else "year" if "year" in w else \
            "week" if "week" in w else "day"  # fmt: skip
    if basis is None and (bb := _BASIS_BEFORE.search(before)):
        basis = bb.group("n").lower()
    pack = None if _PLANNISH.search(before[-40:]) or _PERIODIC.search(after[:80]) else (
        _PACK_AFTER.match(after) or _PACK_BEFORE.search(before))  # fmt: skip
    if basis is None and pack:  # not a plan's allowance ("$0 for 1,000 API calls per month")
        basis, quantity = _pack(pack.group(0))

    # What it pays for, from the words next to it.
    report = bool(_REPORT.search(near) or _REPORT.search(before[-80:]))
    if report:
        kind = PricingType.ONE_TIME
        ambiguities.append(REPORT_PRICE)
    elif _SETUP.search(near):
        kind = PricingType.SETUP
    elif _MINIMUM.search(near):
        kind = PricingType.MINIMUM
    elif _ADD_ON.search(near) or _EXTRA.search(before[-40:]):
        kind = PricingType.ADD_ON
    elif r.percent or basis in _TRANSACTION or _TRANSACTION_CUE.search(near) or paired:
        kind = PricingType.TRANSACTION
    elif basis in _METERED or _USAGE.search(near) or pack:
        kind = PricingType.USAGE
    elif basis in _GOODS:
        kind = PricingType.UNIT
    elif _ONE_TIME.search(near):
        kind = PricingType.ONE_TIME
    elif period or basis in _SEAT or _SUBSCRIPTION.search(near) or ctx.plan:
        kind = PricingType.SUBSCRIPTION
    else:
        kind = PricingType.UNKNOWN
        ambiguities.append("what the amount pays for is not stated")
    if kind is PricingType.TRANSACTION and basis is None and (paired or r.percent):
        basis = "transaction" if re.search(r"\btransaction", near, re.I) else basis
    if kind in (PricingType.SETUP, PricingType.ONE_TIME):
        period = period if kind is PricingType.ONE_TIME else None
    if kind is PricingType.SUBSCRIPTION and period is None:
        ambiguities.append("no billing period stated")

    tail = after[r.consumed :]
    billed = None
    for scope_text in (tail, before):
        b = _BILLED.search(scope_text)
        if b and not _NOT_BILLED.search(scope_text[: b.start()]):
            word = (b.group("a") or b.group("c") or b.group("d") or b.group("m") or "").lower()
            billed = ("monthly" if "month" in word else "quarterly" if "quarter" in word
                      else "annually")  # fmt: skip
            break
    # A table's "Monthly" / "Annual" column, a "Monthly:" label: the billing, not a unit.
    if billed is None and not r.period and (w := _BILLED_WORD.search(f"{before[-30:]} {labels}")):
        word = w.group("w").lower()
        billed = "monthly" if word == "monthly" else "quarterly" if word == "quarterly" \
            else "annually"  # fmt: skip
    if billed and kind is PricingType.UNKNOWN:
        # "($49 billed annually)", an "Annual" column: recurring, though its period is not stated.
        kind = PricingType.SUBSCRIPTION
        ambiguities.remove("what the amount pays for is not stated")
        ambiguities.append("no billing period stated")

    bound = PriceBound.EXACT
    prev_gap = text[reads[i - 1].m.end() : r.m.start()] if i else ""
    if _FROM.search(before) or (i + 1 < len(reads) and _RANGE_GAP.match(
            text[r.m.end() : reads[i + 1].m.start()]) and not reads[i + 1].percent):  # fmt: skip
        bound = PriceBound.FROM
    elif _UP_TO.search(before) or (i and _RANGE_GAP.match(prev_gap) and not r.percent):
        bound = PriceBound.UP_TO
    elif _ABOUT.search(before):
        bound = PriceBound.ABOUT
    if bound is PriceBound.FROM:
        ambiguities.append("a starting price, not every customer's price")
    elif bound is PriceBound.UP_TO:
        ambiguities.append("an upper bound")
    elif bound is PriceBound.ABOUT:
        ambiguities.append("an approximate amount")

    promotional = bool(_PROMO.search(near) or _NOW.search(before))
    if promotional:
        ambiguities.append("a promotional price")
    lead = _label(before, ctx.vendor)
    paren = _PAREN_PLAN.match(tail)
    paren_plan = paren.group("p") if paren and paren.group("p").split()[0].lower() not in \
        _NOT_PLAN and not re.fullmatch(r"[A-Z]{3}", paren.group("p")) else None  # fmt: skip
    product = ctx.product
    named_here = False  # the product is named by the price's own clause
    if ctx.third_party and (owner := lead or _owner(before) or _lead(text, ctx.vendor)):
        product, lead, named_here = owner, None, True  # "Moosend: $9/month" on a comparison
    elif not ctx.third_party and lead and ctx.plan and product is None and kind in (
            PricingType.SUBSCRIPTION, PricingType.UNKNOWN) and _apart(lead, ctx.plan,
                                                                      ctx.vendor):  # fmt: skip
        product, lead, named_here = lead, None, True  # "Advanced Payments ($20/mo)" on a card
    elif kind is PricingType.ADD_ON and lead and product is None:
        product, lead, named_here = lead, None, True  # "Team Members ($11/mo)" under "Add-ons"
    listed = kind is PricingType.ADD_ON and add_on_heading(ctx.labels)
    if product is None and lead is None and (subject := _subject(before, listed)):
        product, named_here = subject, True  # "Vine tomatoes - $3.99/kg"
    if (
        not ctx.third_party
        and product
        and ctx.plan
        and kind in (PricingType.SUBSCRIPTION, PricingType.UNKNOWN)
    ):
        if kind is PricingType.UNKNOWN:
            ambiguities.remove("what the amount pays for is not stated")
        kind = PricingType.ADD_ON  # priced on a plan, named apart from it
    named_plan = _plan(before, ctx.vendor) or _plan(after[:60], ctx.vendor)  # "X plan"
    clause_plan = named_plan or paren_plan or lead
    plan = clause_plan or ctx.plan
    if kind is PricingType.ADD_ON and product is None and ctx.plan and \
            ctx.plan_from is Attribution.HEADING and clause_plan != ctx.plan:  # fmt: skip
        # Under its own heading an add-on is named by the heading; the plan it is
        # bought on is the one its clause names ("... on top of your Business plan").
        product, plan = ctx.plan, clause_plan
    if kind in (PricingType.UNIT, PricingType.USAGE, PricingType.ONE_TIME) and product is None \
            and plan and not named_plan and plan.split()[-1].lower() not in TIERS:  # fmt: skip
        product, plan = plan, None  # goods, metered use, a one-off item: not plans
    plan = debrand(plan, ctx.brands)
    if product is not None and brand_key(product) in ctx.brands:
        product = None  # the vendor itself
    if ctx.third_party:
        if product and plan is None and (split := _split_tier(product)):
            product, plan = split  # "PingWatch Pro"
        elif plan and product is None and (split := _split_tier(plan)):
            product, plan = split
    if plan is not None and product is not None and plan.lower() == product.lower():
        plan = None
    if kind is PricingType.ADD_ON and (a := _ADD_ON_NAME.search(f"{before[-60:]} {after[:40]}")):
        product = a.group("p")
    if report:
        product, named_here = "research report", True
    attribution = None
    if plan or product:
        attribution = Attribution.CLAUSE if clause_plan or named_here else \
            ctx.plan_from or Attribution.HEADING  # fmt: skip
    included = None
    for scope_text in (tail[:100], before[-80:]):
        if inc := _INCLUDED.search(scope_text):
            included = (inc.group("a") or inc.group("b")).strip()
            break
    tax_text = f"{tail[:80]} {labels}" if len(reads) > 1 else f"{text} {labels}"
    tax = "excluded" if _TAX_EX.search(tax_text) else "included" if _TAX_IN.search(tax_text) \
        else None  # fmt: skip
    eff = EFFECTIVE.search(text)
    effective = eff.group("d") if eff else ctx.effective

    end = min(r.m.end() + r.consumed, own)
    unit = "/".join([
        "percent" if r.percent else currency or "unknown",
        *([f"{quantity:g} {basis}" if quantity else basis] if basis else []),
        *([period] if period else []),
    ])  # fmt: skip
    return Metric(
        kind=MetricKind.PRICE_POINT, value=round(r.value, 10), unit=unit,
        text=text[r.m.start() : end].rstrip(),
        price=Price(pricing_type=kind, currency=currency, currency_assumed=assumed,
                    percent=r.percent, period=period, billed=billed, basis=basis,
                    basis_quantity=quantity, bound=bound, plan=plan, product=product,
                    vendor=ctx.vendor, attribution=attribution, promotional=promotional,
                    tax=tax, included=included,
                    effective=effective, context=list(ctx.context), ambiguities=ambiguities),
    )  # fmt: skip


def _on_request(text: str, ctx: Context, own: int) -> list[Metric]:
    m = _QUOTE.search(text[:own])
    if m is None or not (_QUOTE_CONTEXT.search(text) or ctx.plan or ctx.card):
        return []
    named = _plan(text, ctx.vendor)
    plan = debrand(named or ctx.plan, ctx.brands)
    product = None if ctx.product and brand_key(ctx.product) in ctx.brands else ctx.product
    attribution = (Attribution.CLAUSE if named else ctx.plan_from or Attribution.HEADING) \
        if plan or product else None  # fmt: skip
    return [Metric(
        kind=MetricKind.PRICE_ON_REQUEST, value=0.0, unit="none", text=m.group(0),
        price=Price(pricing_type=PricingType.QUOTE, currency=None, plan=plan,
                    product=product, vendor=ctx.vendor, attribution=attribution,
                    context=list(ctx.context), ambiguities=["no amount: quote-based"]),
    )]  # fmt: skip


# A page's "last updated" dates the page: on a vendor's own pricing page that is its
# prices, on an article it is the article (its prices need "prices as of" wording).
_PRICE_EFFECTIVE = re.compile(EFFECTIVE.pattern.replace("|last updated|updated on|verified", ""),
                              re.I)  # fmt: skip


def page_effective(texts: list[str], pricing_page: bool = True) -> str | None:
    """The page's "prices as of / last updated" date, if one is stated."""
    rule = EFFECTIVE if pricing_page else _PRICE_EFFECTIVE
    for t in texts:
        if m := rule.search(t):
            return m.group("d")
    return None


def pricing_page(url: str, title: str | None) -> bool:
    """One vendor's pricing page (by its path or title), not a comparison of several."""
    texts = (urlsplit(url).path.replace("-", " ").replace("/", " "), title or "")
    return any(PRICING_PAGE.search(t) for t in texts) and \
        not any(_ROUNDUP.search(t) for t in texts)  # fmt: skip
