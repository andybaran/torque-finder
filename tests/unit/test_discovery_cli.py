from __future__ import annotations


def test_build_parser_seed_command():
    from parts_lookup.discovery.cli import build_parser

    args = build_parser().parse_args(["seed", "ed-red-e1", "cn-red-e1"])
    assert args.command == "seed"
    assert args.model_ids == ["ed-red-e1", "cn-red-e1"]


def test_build_parser_sitemap_command():
    from parts_lookup.discovery.cli import build_parser

    args = build_parser().parse_args(["sitemap"])
    assert args.command == "sitemap"
