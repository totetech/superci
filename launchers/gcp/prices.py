# Hourly prices in US dollars of the machine types the launcher can pick, in us-west4 (Las Vegas).
# Source: the gcloud-compute.com dataset (https://gcloud-compute.com/machine-types-regions.csv), rows with
# region "us-west4"; SPOT is its hourSpot column and ON_DEMAND its hour column, each rounded to four decimals.
# GCE prices change, so this is a snapshot and `price` reports an estimate. To refresh it, download the
# dataset again, take the same rows, and set TAKEN to the date the dataset states.
TAKEN = "2026-11-01"

SPOT: dict[str, float] = {
    # e2
    "e2-standard-2": 0.0104,
    "e2-standard-4": 0.0209,
    "e2-standard-8": 0.0417,
    "e2-standard-16": 0.0834,
    "e2-standard-32": 0.1668,
    # n2d
    "n2d-standard-2": 0.0268,
    "n2d-standard-4": 0.0535,
    "n2d-standard-8": 0.107,
    "n2d-standard-16": 0.214,
    "n2d-standard-32": 0.4281,
    "n2d-standard-64": 0.8562,
    # c4d
    "c4d-standard-2": 0.0165,
    "c4d-standard-4": 0.0337,
    "c4d-standard-8": 0.068,
    "c4d-standard-16": 0.136,
    "c4d-standard-32": 0.2721,
    "c4d-standard-64": 0.5442,
}

ON_DEMAND: dict[str, float] = {
    # e2
    "e2-standard-2": 0.0755,
    "e2-standard-4": 0.1509,
    "e2-standard-8": 0.3019,
    "e2-standard-16": 0.6038,
    "e2-standard-32": 1.2075,
    # n2d
    "n2d-standard-2": 0.0952,
    "n2d-standard-4": 0.1903,
    "n2d-standard-8": 0.3806,
    "n2d-standard-16": 0.7613,
    "n2d-standard-32": 1.5226,
    "n2d-standard-64": 3.0451,
    # c4d
    "c4d-standard-2": 0.1033,
    "c4d-standard-4": 0.2107,
    "c4d-standard-8": 0.4257,
    "c4d-standard-16": 0.8514,
    "c4d-standard-32": 1.7029,
    "c4d-standard-64": 3.4058,
}
