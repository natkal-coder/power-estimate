"""Household electricity price per kWh by ISO country code, for a default only.

National averages, not your contract: pass --tariff for a real bill. Keep only
openly reusable sources here. Values from sites licensed NonCommercial or
NoDerivs must not be added, since this repo is MIT.
"""
SG = 0.3116  # SP Group with GST, 1 Oct-31 Dec 2026
US = 0.182  # EIA Electric Power Monthly, residential, Jan-Aug 2026
GB = 0.2632  # Ofgem price cap unit rate, 1 Oct-31 Dec 2026, excludes standing charge

# Eurostat nrg_pc_204, 2026-S1, 2500-4999 kWh a year, all taxes, in EUR for every
# country listed, including those that do not use the euro.
EUR = {
    "AL": 0.119, "AT": 0.326, "BA": 0.0995, "BE": 0.3575, "BG": 0.1341, "CY": 0.2688,
    "CZ": 0.2884, "DE": 0.3618, "DK": 0.2375, "EE": 0.2118, "ES": 0.2509, "FI": 0.2694,
    "FR": 0.2485, "GE": 0.0731, "GR": 0.2401, "HR": 0.1816, "HU": 0.1139, "IE": 0.3871,
    "IS": 0.2076, "IT": 0.3142, "LI": 0.2988, "LT": 0.2162, "LU": 0.2023, "LV": 0.2415,
    "MD": 0.1872, "ME": 0.1039, "MK": 0.1168, "MT": 0.1268, "NL": 0.244, "NO": 0.1993,
    "PL": 0.2999, "PT": 0.2367, "RO": 0.2836, "RS": 0.1197, "SE": 0.3062, "SI": 0.2118,
    "SK": 0.1985, "TR": 0.0657,
}


def regional_rate(code):
    """Return (price per kWh, currency symbol), or None if no open rate is held."""
    if code in EUR:
        return EUR[code], "€"
    return {"SG": (SG, "S$"), "US": (US, "$"), "GB": (GB, "£")}.get(code)
