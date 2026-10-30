"""Goods spelled differently on different invoices ("Putti" / "Putty" / "Phutty") are ONE goods everywhere: the figures kept
for a factory, the Factories tab, figures sent on WhatsApp or by email, and profiles saved before this was so (merged
once at startup). Runs on an in-memory database."""

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import GoodsProfile, InvoiceRecord, InvoiceStatus
from app.routers import factories
from app.services import goods_profiles, stock_update

CUSTOMER = "Mian Cotton Ginning Pressing & Oil Mills"
KEY = goods_profiles.customer_key({"customer_name": CUSTOMER})


def order(*goods):
    return {"customer_name": CUSTOMER, "currency": "PKR", "rows": [{"description": g} for g in goods]}


class InMemory(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        patch = mock.patch.object(goods_profiles, "SessionLocal", self.Session)
        patch.start()
        self.addCleanup(patch.stop)

    def profiles(self):
        s = self.Session()
        self.addCleanup(s.close)
        return {p.goods_key: p for p in s.query(GoodsProfile)}, s


class Keys(unittest.TestCase):
    def test_spelling_variants_share_one_key(self):
        keys = {goods_profiles.goods_key(d) for d in ("Putti (munds)", "Putty", "PHUTTY (season 2026-27)", "putti")}
        self.assertEqual(len(keys), 1)

    def test_different_goods_stay_apart_and_wheat_variants_still_group(self):
        self.assertNotEqual(goods_profiles.goods_key("Putti"), goods_profiles.goods_key("Cotton"))
        self.assertEqual(goods_profiles.goods_key("Wheat Bags 50Kgs"), goods_profiles.goods_key("Wheat Hgr 20Kgs"))

    def test_the_label_keeps_the_documents_spelling(self):
        self.assertEqual(goods_profiles.goods_label("Putty (munds)"), "Putty")
        self.assertEqual(goods_profiles.goods_label("Cotton (Bales)"), "Cotton")


class SharedFigures(InMemory):
    def given(self, description, **figures):
        goods_profiles.remember({**order(description), "rows": [{"description": description, **figures}]})

    def test_figures_given_for_putti_are_used_for_putty_and_not_asked_again(self):
        self.given("Putti (munds)", margin_pct="20", rate_per_unit="3800")
        later = order("Putty")
        used = goods_profiles.apply(later)
        self.assertEqual(later["rows"][0]["margin_pct"], "20")
        self.assertEqual(later["rows"][0]["rate_per_unit"], "3800")
        self.assertTrue(used)

    def test_one_profile_not_two(self):
        self.given("Putti", margin_pct="20")
        self.given("Putty", margin_pct="25")
        profiles, _ = self.profiles()
        self.assertEqual(len(profiles), 1)
        (profile,) = profiles.values()
        self.assertEqual((profile.margin_pct, profile.goods_label), ("25", "Putti"))  # newest figure, first spelling

    def test_whatsapp_figures_typed_with_another_spelling_reach_the_same_goods(self):
        self.given("Putti", margin_pct="20")
        with self.Session() as s:
            notes = stock_update.save_profile_figures(s, KEY, CUSTOMER, [("margin", "putty", "30"), ("rate", "putty", "3900")])
            s.commit()
        profiles, _ = self.profiles()
        self.assertEqual(len(profiles), 1)
        (profile,) = profiles.values()
        self.assertEqual((profile.margin_pct, profile.rate_per_unit), ("30", "3900"))
        self.assertTrue(any("Putti" in n for n in notes), notes)  # reported under the name it is kept as

    def test_a_new_goods_typed_by_whatsapp_gets_a_readable_name(self):
        with self.Session() as s:
            notes = stock_update.save_profile_figures(s, KEY, CUSTOMER, [("margin", "phutty", "30")])
            s.commit()
        profiles, _ = self.profiles()
        self.assertEqual(next(iter(profiles.values())).goods_label, "Phutty")
        self.assertIn("Phutty — Margin %: 30", notes)


class MergingOldProfiles(InMemory):
    def add(self, key, when, **figures):
        with self.Session() as s:
            s.add(GoodsProfile(customer_key=KEY, goods_key=key, customer_name=CUSTOMER,
                               updated_at=datetime(2026, 10, 1) + timedelta(days=when), **figures))
            s.commit()

    def test_putti_and_putty_rows_become_one_with_the_newest_figures(self):
        self.add("putti", 1, margin_pct="20", rate_per_unit="3800")
        self.add("putty", 2, margin_pct="25")
        changed = goods_profiles.migrate_keys()
        profiles, _ = self.profiles()
        self.assertGreaterEqual(changed, 1)
        self.assertEqual(list(profiles), [goods_profiles.goods_key("putti")])
        (profile,) = profiles.values()
        self.assertEqual(profile.margin_pct, "25")  # the newer
        self.assertEqual(profile.rate_per_unit, "3800")  # kept from the older: the newer had none
        self.assertEqual(profile.goods_label, "Putti")  # the first spelling it was kept under

    def test_other_factories_and_goods_are_untouched(self):
        self.add("putti", 1, margin_pct="20")
        self.add("cotton", 1, margin_pct="25")
        with self.Session() as s:
            s.add(GoodsProfile(customer_key="otherfactory", goods_key="putty", margin_pct="5"))
            s.commit()
        goods_profiles.migrate_keys()
        _, s = self.profiles()
        self.assertEqual(s.query(GoodsProfile).count(), 3)  # putti, cotton, and the other factory's putty
        self.assertEqual(s.query(GoodsProfile).filter_by(customer_key="otherfactory").one().margin_pct, "5")

    def test_running_it_again_changes_nothing(self):
        self.add("putti", 1, margin_pct="20")
        self.add("putty", 2, margin_pct="25")
        goods_profiles.migrate_keys()
        self.assertEqual(goods_profiles.migrate_keys(), 0)

    def test_a_failure_does_not_stop_the_app(self):
        with mock.patch.object(goods_profiles, "SessionLocal", side_effect=RuntimeError("db down")):
            self.assertEqual(goods_profiles.migrate_keys(), 0)


class FactoriesTab(InMemory):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(factories.router)

        def db():
            s = self.Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = db
        self.client = TestClient(app)
        with self.Session() as s:
            for i, goods in enumerate(["Putti (munds)", "Putty (munds)", "Cotton (Bales)"], start=1):
                body = {"delivery_order": {**order(goods), "rows": [{"description": goods, "opening_balance": "10"}]}}
                s.add(InvoiceRecord(id=i, email_id=1, pdf_filename=f"h_{i}.pdf", status=InvoiceStatus.CONFIRMED,
                                    raw_extracted_json=json.dumps(body)))
            s.commit()

    def goods(self):
        (factory,) = self.client.get("/api/factories").json()["factories"]
        return factory["goods"]

    def test_putti_and_putty_invoices_make_one_entry_not_two(self):
        goods = self.goods()
        self.assertEqual(len(goods), 2)  # putti/putty and cotton
        self.assertEqual(next(g for g in goods if g["label"] == "Putti")["orders"], [1, 2])

    def test_the_entry_is_named_as_the_documents_spell_it_not_as_the_internal_key(self):
        self.assertEqual(sorted(g["label"] for g in self.goods()), ["Cotton", "Putti"])

    def test_saved_figures_show_on_the_one_entry(self):
        self.client.put(f"/api/factories/{KEY}/{goods_profiles.goods_key('Putty')}", json={"margin_pct": "20"})
        goods = self.goods()
        self.assertEqual(len(goods), 2)
        self.assertEqual(next(g for g in goods if g["label"] == "Putti")["margin_pct"], "20")

    def test_an_old_profile_row_is_merged_into_the_same_entry(self):
        with self.Session() as s:
            s.add(GoodsProfile(customer_key=KEY, goods_key="phutty", customer_name=CUSTOMER, margin_pct="22"))
            s.commit()
        goods_profiles.migrate_keys()
        goods = self.goods()
        self.assertEqual(len(goods), 2)
        self.assertEqual(next(g for g in goods if g["orders"] == [1, 2])["margin_pct"], "22")


if __name__ == "__main__":
    unittest.main()
