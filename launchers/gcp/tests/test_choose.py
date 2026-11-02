import pytest

from launcher import FAMILIES, SIZES, choose, price
from prices import ON_DEMAND, SPOT


def test_the_smallest_size_that_fits_in_each_family_in_order():
    assert choose(2, 8) == ["e2-standard-2", "c4d-standard-2", "n2d-standard-2"]
    assert choose(4, 16) == ["e2-standard-4", "c4d-standard-4", "n2d-standard-4"]
    assert choose(3, 8) == ["e2-standard-4", "c4d-standard-4", "n2d-standard-4"]
    assert choose(2, 12) == ["e2-standard-4", "c4d-standard-4", "n2d-standard-4"]
    assert choose(64, 256) == ["c4d-standard-64", "n2d-standard-64"]
    assert choose(65, 8) == []


def test_families_are_in_spot_price_order_at_every_size_they_share():
    for earlier, later in zip(FAMILIES, FAMILIES[1:]):
        for n in set(SIZES[earlier]) & set(SIZES[later]):
            assert SPOT[f"{earlier}-standard-{n}"] < SPOT[f"{later}-standard-{n}"], (earlier, later, n)


def test_every_size_of_every_family_has_both_prices():
    for family, sizes in SIZES.items():
        for n in sizes:
            t = f"{family}-standard-{n}"
            assert t in SPOT and t in ON_DEMAND and 0 < SPOT[t] < ON_DEMAND[t], t


def test_known_prices():
    assert (SPOT["e2-standard-2"], ON_DEMAND["e2-standard-2"]) == (0.0104, 0.0755)
    assert (SPOT["c4d-standard-2"], ON_DEMAND["c4d-standard-2"]) == (0.0165, 0.1033)
    assert price("e2-standard-2", spot=False) == 0.0755
    with pytest.raises(KeyError):
        price("n1-standard-1", spot=True)
