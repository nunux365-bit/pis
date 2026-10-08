"""Plant code prefix ↔ purchasing organisation rules."""

from app.procurement.plant_org import default_plant_for_org, plant_matches_purchasing_org


def test_plant_prefix_h_only_1mgh() -> None:
    assert plant_matches_purchasing_org("H002", "1MGH")
    assert not plant_matches_purchasing_org("H002", "1MGT")
    assert not plant_matches_purchasing_org("H002", "1LFS")


def test_plant_prefix_l_only_1lfs() -> None:
    assert plant_matches_purchasing_org("L001", "1LFS")
    assert not plant_matches_purchasing_org("L001", "1MGH")


def test_plant_prefix_t_only_1mgt() -> None:
    assert plant_matches_purchasing_org("T003", "1MGT")
    assert not plant_matches_purchasing_org("T003", "1LFS")


def test_plant_other_prefix_all_orgs() -> None:
    assert plant_matches_purchasing_org("0001", "1MGH")
    assert plant_matches_purchasing_org("0001", "1MGT")
    assert plant_matches_purchasing_org("0001", "1LFS")


def test_default_plant_for_org_picks_first_match() -> None:
    codes = ["0001", "H002", "T001", "L001"]
    assert default_plant_for_org(codes, "1MGH") == "H002"
    assert default_plant_for_org(codes, "1MGT") == "T001"
    assert default_plant_for_org(codes, "1LFS") == "L001"
