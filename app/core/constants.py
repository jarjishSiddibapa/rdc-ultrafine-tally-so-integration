"""GST state codes (as used on GSTIN prefixes)."""
from __future__ import annotations

GST_STATES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}

STATE_CHOICES = sorted(GST_STATES.items(), key=lambda kv: kv[1])

GST_RATES = ["0", "5", "12", "18", "28"]

# Units of measurement an item can be sold in (Tally unit symbol, description).
UNITS: dict[str, str] = {
    "MT": "Metric tonne", "KG": "Kilogram", "NOS": "Numbers", "PCS": "Pieces", "BAG": "Bags",
    "BOX": "Boxes", "LTR": "Litres", "SQM": "Square metre", "CUM": "Cubic metre",
}
