"""Setup (admin): restaurant configuration, imports, users and restore."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

from restaurant_ops import admin as ADMIN
from restaurant_ops.config import settings
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import ops as OPS
from restaurant_ops.live.api.deps import ADMINONLY, MANAGER, _admin, guard
from restaurant_ops.live.api.hub import hub
from restaurant_ops.live.api.models import HoursReq, ImportReq, ItemPriceReq, RosterReq, UserPatch, UserReq

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

@router.get("/api/admin/config")
async def admin_config(user: dict = ADMINONLY):
    return _admin(lambda m: {**ADMIN.snapshot(m), "applies_from": "the next service (Start a new service to apply)",
                             "current_service_started": hub.svc.info()["start"] if hub.svc else None})


@router.put("/api/admin/profile")
async def admin_profile(body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.set_profile(m, body))


@router.post("/api/admin/taxes")
async def admin_tax_new(body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_tax(m, body))


@router.put("/api/admin/taxes/{tax_id}")
async def admin_tax_put(tax_id: int, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_tax(m, body, tax_id))


@router.delete("/api/admin/taxes/{tax_id}")
async def admin_tax_del(tax_id: int, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_tax(m, tax_id) or {"deleted": tax_id})


@router.put("/api/admin/hours")
async def admin_hours(req: HoursReq, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.set_hours(m, req.weekday, req.meal_period, req.open, req.close, req.closed))


@router.put("/api/admin/tables/{table_id}")
async def admin_table(table_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_table(m, {**body, "id": table_id}))


@router.delete("/api/admin/tables/{table_id}")
async def admin_table_del(table_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_table(m, table_id) or {"deleted": table_id})


@router.put("/api/admin/stations/{station_id}")
async def admin_station(station_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_station(m, {**body, "id": station_id}))


@router.delete("/api/admin/stations/{station_id}")
async def admin_station_del(station_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_station(m, station_id) or {"deleted": station_id})


@router.put("/api/admin/zones/{zone_id}")
async def admin_zone(zone_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_zone(m, {**body, "id": zone_id}))


@router.delete("/api/admin/zones/{zone_id}")
async def admin_zone_del(zone_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_zone(m, zone_id) or {"deleted": zone_id})


@router.put("/api/admin/ingredients/{ingredient_id}")
async def admin_ingredient(ingredient_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_ingredient(m, {**body, "id": ingredient_id}))


@router.delete("/api/admin/ingredients/{ingredient_id}")
async def admin_ingredient_del(ingredient_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_ingredient(m, ingredient_id) or {"deleted": ingredient_id})


@router.put("/api/admin/dishes/{dish_id}")
async def admin_dish(dish_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_dish(m, {**body, "id": dish_id}))


@router.post("/api/admin/dishes/{dish_id}/{action}")
async def admin_dish_action(dish_id: str, action: str, user: dict = ADMINONLY):
    if action not in ("retire", "restore"):
        raise HTTPException(404, "action must be retire or restore")
    return _admin(lambda m: ADMIN.retire_dish(m, dish_id, active=action == "restore"))


@router.put("/api/admin/suppliers/{supplier_id}")
async def admin_supplier(supplier_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_supplier(m, {**body, "id": supplier_id}))


@router.put("/api/admin/suppliers/{supplier_id}/items/{ingredient_id}")
async def admin_supplier_item(supplier_id: str, ingredient_id: str, req: ItemPriceReq, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_supplier_item(m, supplier_id, ingredient_id, req.price, req.pack_size))


@router.delete("/api/admin/suppliers/{supplier_id}/items/{ingredient_id}")
async def admin_supplier_item_del(supplier_id: str, ingredient_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_supplier_item(m, supplier_id, ingredient_id) or {"deleted": ingredient_id})


@router.post("/api/admin/staff")
async def admin_staff_new(body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_staff(m, body))


@router.put("/api/admin/staff/{staff_id}")
async def admin_staff(staff_id: int, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_staff(m, body, staff_id))


@router.get("/api/admin/roster")
async def admin_roster(date: str, meal_period: str = "dinner", user: dict = MANAGER):
    return _admin(lambda m: ADMIN.get_roster(m, date, meal_period))


@router.put("/api/admin/roster")
async def admin_roster_put(req: RosterReq, user: dict = MANAGER):
    return _admin(lambda m: ADMIN.set_roster(m, req.date, req.meal_period, req.entries))


@router.post("/api/admin/import/{kind}")
async def admin_import(kind: str, req: ImportReq, user: dict = ADMINONLY):
    from restaurant_ops.data import importer as IMP

    if kind not in ("menu", "inventory", "pos"):
        raise HTTPException(404, "kind must be menu, inventory or pos")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{kind}.csv"
        path.write_text(req.csv)
        if kind == "menu":
            return guard(lambda: IMP.import_menu(path, settings.DB_PATH, req.dry_run))
        if kind == "inventory":
            return guard(lambda: IMP.import_inventory(path, settings.DB_PATH, replace=req.replace_stock, dry_run=req.dry_run))
        return guard(lambda: IMP.import_pos(path, settings.DB_PATH, dry_run=req.dry_run))


@router.get("/api/admin/users")
async def admin_users(user: dict = ADMINONLY):
    return _admin(AUTH.list_users)


@router.post("/api/admin/users")
async def admin_user_new(req: UserReq, user: dict = ADMINONLY):
    return _admin(lambda m: AUTH.create_user(m, req.username, req.password, req.role, req.display_name))


@router.patch("/api/admin/users/{user_id}")
async def admin_user_patch(user_id: int, req: UserPatch, user: dict = ADMINONLY):
    return _admin(lambda m: AUTH.update_user(m, user_id, req.role, req.active, req.password, req.display_name))


@router.post("/api/admin/restore/{backup_id}")
async def admin_restore(backup_id: str, user: dict = ADMINONLY):
    """Restore a verified backup in place: the current service is saved and stopped first, then a fresh one starts."""
    v = OPS.verify_backup(backup_id)
    if not v["ok"]:
        raise HTTPException(400, f"backup failed verification: {v['corrupt_or_missing']}")
    if hub.svc:
        hub.svc.save_state()
        hub.svc.close()
        hub.svc = None
    out = OPS.restore(backup_id)
    hub.start_service()
    return out
