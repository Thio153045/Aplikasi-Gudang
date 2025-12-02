# streamlit_gudang_supabase.py
# Versi lengkap: Streamlit app terhubung ke Supabase (mengganti SQLite)
#
# SEBELUM MENJALANKAN:
# 1) Buat tabel di Supabase SQL Editor (copy-paste dan RUN):
# ----------------------------------------------------------------
# CREATE TABLE IF NOT EXISTS users (
#   username TEXT PRIMARY KEY,
#   password_hash TEXT NOT NULL
# );
#
# CREATE TABLE IF NOT EXISTS items (
#   id SERIAL PRIMARY KEY,
#   name TEXT NOT NULL,
#   category TEXT,
#   unit TEXT,
#   quantity DOUBLE PRECISION DEFAULT 0,
#   min_stock DOUBLE PRECISION DEFAULT 0,
#   rack_location TEXT,
#   expiry_date DATE,
#   created_at TIMESTAMP,
#   updated_at TIMESTAMP
# );
#
# CREATE TABLE IF NOT EXISTS transactions (
#   id SERIAL PRIMARY KEY,
#   trx_type TEXT NOT NULL,
#   item_id INTEGER,
#   name TEXT,
#   quantity DOUBLE PRECISION,
#   unit TEXT,
#   requester TEXT,
#   supplier TEXT,
#   note TEXT,
#   bundle_code TEXT,
#   trx_code TEXT,
#   expiry_date DATE,
#   created_at TIMESTAMP
# );
# ----------------------------------------------------------------
#
# 2) Atur st.secrets di Streamlit (LOCAL: ~/.streamlit/secrets.toml atau Streamlit Cloud Secrets)
# SUPABASE_URL = 
# SUPABASE_KEY = "cm9sZSI6ImFub24iLCJpYXQiOjE3NjQwNjIzNDIsImV4cCI6MjA3OTYzODM0Mn0.uAW_wTrjEx0vt4hY6SHCtqcEa3Qjtxk2g7GNa7SrRZ4"
#
# 3) Install dependency:
# pip install streamlit pandas openpyxl supabase altair

import streamlit as st
import pandas as pd
import io
from datetime import datetime, timedelta, date
import altair as alt
import random
import hashlib
from supabase import create_client, Client

# -------------------------
# Supabase client (from secrets)
# -------------------------
if "SUPABASE_URL" not in st.secrets or "SUPABASE_KEY" not in st.secrets:
    st.error("SUPABASE_URL dan SUPABASE_KEY belum ada di st.secrets. Silakan tambahkan sebelum menjalankan.")
    st.stop()

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------------------
# Utility: hashing password
# -------------------------
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

# -------------------------
# Ensure default admin exists
# -------------------------
def ensure_default_admin():
    q = supabase.table("users").select("username").limit(1).execute()
    if not q.data:
        supabase.table("users").insert({
            "username": "admin",
            "password_hash": hash_pw("admin123")
        }).execute()

# -------------------------
# Auth
# -------------------------
def verify_login(username: str, password: str) -> bool:
    if not username:
        return False
    pw_hash = hash_pw(password)
    q = supabase.table("users").select("password_hash").eq("username", username).limit(1).execute()
    if not q.data:
        return False
    return q.data[0].get("password_hash") == pw_hash

# -------------------------
# Helpers: items & transactions
# -------------------------
def generate_trx_code(trx_type: str) -> str:
    now = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"TRX-{trx_type.upper()}-{now}-{random.randint(100,999)}"

def get_inventory_df() -> pd.DataFrame:
    q = supabase.table("items").select("*").order("name", desc=False).execute()

    rows = q.data or []
    if not rows:
        return pd.DataFrame(columns=["id","name","category","unit","quantity","min_stock","rack_location","expiry_date","created_at","updated_at"])
    df = pd.DataFrame(rows)
    # Normalize dates
    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce").dt.date
    if "created_at" in df.columns:
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    return df

def get_items_list():
    df = get_inventory_df()
    if df.empty:
        return []
    return df["name"].tolist()

def get_item_unit(name: str):
    if not name:
        return ""
    q = supabase.table("items").select("unit").eq("name", name).limit(1).execute()
    if not q.data:
        return ""
    return q.data[0].get("unit","") or ""

def upsert_item(name, category, unit, quantity, min_stock=0.0, rack_location="", expiry_date=None):
    name = (name or "").strip()
    now = datetime.now().isoformat()
    # find by name+unit
    res = supabase.table("items").select("*").eq("name", name).eq("unit", unit).limit(1).execute()
    if res.data:
        item = res.data[0]
        existing_qty = item.get("quantity") or 0
        new_qty = existing_qty + (quantity or 0)
        supabase.table("items").update({
            "quantity": new_qty,
            "category": category,
            "min_stock": min_stock,
            "rack_location": rack_location,
            "expiry_date": expiry_date.isoformat() if isinstance(expiry_date, (date,)) else expiry_date,
            "updated_at": now
        }).eq("id", item["id"]).execute()
        return item["id"]
    else:
        ins = supabase.table("items").insert({
            "name": name,
            "category": category,
            "unit": unit,
            "quantity": quantity or 0,
            "min_stock": min_stock or 0,
            "rack_location": rack_location,
            "expiry_date": expiry_date.isoformat() if isinstance(expiry_date, (date,)) else expiry_date,
            "created_at": now,
            "updated_at": now
        }).execute()
        return ins.data[0]["id"]

def adjust_item_for_out(name, unit, quantity):
    res = supabase.table("items").select("*").eq("name", name).eq("unit", unit).limit(1).execute()
    if not res.data:
        return None, "Item tidak ditemukan"
    item = res.data[0]
    existing = item.get("quantity") or 0
    if existing < quantity:
        return None, f"Stok tidak cukup: tersedia {existing}"
    new_qty = existing - quantity
    supabase.table("items").update({"quantity": new_qty, "updated_at": datetime.now().isoformat()}).eq("id", item["id"]).execute()
    return item["id"], None

def add_transaction_record(trx_type, item_id, name, quantity, unit, requester, supplier, note, bundle_code, trx_code, expiry_date=None):
    now = datetime.now().isoformat()
    supabase.table("transactions").insert({
        "trx_type": trx_type,
        "item_id": item_id,
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "requester": requester,
        "supplier": supplier,
        "note": note,
        "bundle_code": bundle_code,
        "trx_code": trx_code,
        "expiry_date": expiry_date.isoformat() if isinstance(expiry_date, (date,)) else expiry_date,
        "created_at": now
    }).execute()

# -------------------------
# Load / Export
# -------------------------
def load_inventory_from_excel(buffer) -> int:
    """ buffer can be file-like or BytesIO from uploaded file """
    if isinstance(buffer, io.BytesIO):
        buffer.seek(0)
        df = pd.read_excel(buffer)
    else:
        df = pd.read_excel(buffer)

    df_columns = {c.lower(): c for c in df.columns}
    required = ['name', 'quantity', 'unit']
    for r in required:
        if r not in df_columns:
            raise ValueError(f"Excel harus memiliki kolom: {', '.join(required)}")

    inserted = 0
    for _, row in df.iterrows():
        name = str(row[df_columns['name']]).strip()
        quantity = float(row[df_columns['quantity']]) if not pd.isna(row[df_columns['quantity']]) else 0.0
        unit = str(row[df_columns['unit']]).strip()
        category = str(row[df_columns['category']]).strip() if 'category' in df_columns else ''
        min_stock = float(row[df_columns['min_stock']]) if 'min_stock' in df_columns and not pd.isna(row[df_columns['min_stock']]) else 0.0
        rack_location = str(row[df_columns['rack_location']]).strip() if 'rack_location' in df_columns else ''
        expiry_date = None
        if 'expiry_date' in df_columns and not pd.isna(row[df_columns['expiry_date']]):
            val = row[df_columns['expiry_date']]
            if isinstance(val, (pd.Timestamp, datetime, date)):
                expiry_date = val.date() if isinstance(val, pd.Timestamp) else (val if isinstance(val, date) else None)
            else:
                # try parse
                try:
                    expiry_date = pd.to_datetime(val).date()
                except:
                    expiry_date = None

        upsert_item(name, category, unit, quantity, min_stock, rack_location, expiry_date)
        inserted += 1

    return inserted

def export_db_to_excel_bytes():
    items_q = supabase.table("items").select("*").order("name", {"ascending": True}).execute()
    trans_q = supabase.table("transactions").select("*").order("created_at", {"ascending": False}).execute()
    df_items = pd.DataFrame(items_q.data or [])
    df_trans = pd.DataFrame(trans_q.data or [])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_items.to_excel(writer, sheet_name="inventory", index=False)
        df_trans.to_excel(writer, sheet_name="transactions", index=False)
        writer.save()
    return output.getvalue()

# -------------------------
# Reporting helpers
# -------------------------
def load_transactions_df():
    q = supabase.table("transactions").select("*").order("created_at", desc=False).execute()
    df = pd.DataFrame(q.data or [])
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["date"] = df["created_at"].dt.date
    df["month"] = df["created_at"].dt.to_period("M").dt.to_timestamp()
    df["week"] = df["created_at"].dt.to_period("W").dt.start_time
    return df

def totals_for_period(df):
    if df.empty:
        return pd.DataFrame()

    # pastikan kolom date sudah datetime
    df['date'] = pd.to_datetime(df['date'])

    # Mingguan
    if period == 'W':
        df['week'] = df['date'].dt.strftime('%Y-%W')
        g = df.groupby(['week', 'name', 'unit', 'trx_type'])['quantity'].sum().reset_index()
        return g

    # Bulanan
    if period == 'M':
        df['month'] = df['date'].dt.strftime('%Y-%m')
        g = df.groupby(['month', 'name', 'unit', 'trx_type'])['quantity'].sum().reset_index()
        return g

    return pd.DataFrame()



# --- Login ---
if not st.session_state.auth:
    st.title("Login - Aplikasi Gudang (Supabase)")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
        if submitted:
            if verify_login(username, password):
                st.session_state.auth = True
                st.session_state.user = username
                st.success(f"Login sukses sebagai {username}")
                st.rerun()
            else:
                st.error("Username atau password salah")
    st.info("Default akun: admin / admin123 (ubah setelah login)")
    st.stop()

# Main UI after login
st.sidebar.title("Menu")
menu = st.sidebar.radio("Pilih", ["Dashboard", "Upload Inventaris (Excel)", "Barang Masuk", "Barang Keluar", "Laporan & Analisis", "Pengaturan"])
st.sidebar.write("User:", st.session_state.user)
if st.sidebar.button("Logout"):
    st.session_state.auth = False
    st.session_state.user = None
    st.created_atrerun()

# --- Dashboard ---
if menu == "Dashboard":
    st.title("Dashboard Gudang")
    inv = get_inventory_df()
    st.subheader("Inventaris")
    if inv.empty:
        st.info("Inventaris kosong. Silakan upload data awal.")
    else:
        st.dataframe(inv)
        low = inv[inv["quantity"] <= inv["min_stock"]]
        if not low.empty:
            st.warning("Beberapa item mencapai atau di bawah min stock:")
            st.table(low[["name","quantity","min_stock","unit"]])

    st.subheader("Transaksi Terakhir")
    trans_q = (supabase.table("transactions").select("*") .order("created_at", desc=True) .limit(20).execute())

    trans_df = pd.DataFrame(trans_q.data or [])
    st.dataframe(trans_df)

    st.subheader("Total per Item (seluruh waktu)")
    df_all = load_transactions_df()
    totals_all = totals_for_period(df_all)
    st.dataframe(totals_all)

    if not df_all.empty:
        in_all = df_all[df_all["trx_type"]=="in"].groupby("name")["quantity"].sum().reset_index()
        out_all = df_all[df_all["trx_type"]=="out"].groupby("name")["quantity"].sum().reset_index()
        st.markdown("Grafik Total Masuk (seluruh waktu)")
        c = alt.Chart(in_all).mark_bar().encode(x="name:N", y="quantity:Q").properties(height=300).interactive()
        st.altair_chart(c, use_container_width=True)
        st.markdown("Grafik Total Keluar (seluruh waktu)")
        c2 = alt.Chart(out_all).mark_bar().encode(x="name:N", y="quantity:Q").properties(height=300).interactive()
        st.altair_chart(c2, use_container_width=True)

# --- Upload Inventaris ---
elif menu == "Upload Inventaris (Excel)":
    st.title("Upload Inventaris Awal dari Excel/CSV")
    st.markdown("Format minimal: kolom `name`, `quantity`, `unit`. Optional: `category`, `min_stock`, `rack_location`, `expiry_date`")
    uploaded = st.file_uploader("Pilih file Excel (.xlsx) atau CSV", type=["xlsx","xls","csv"])
    if uploaded:
        try:
            if uploaded.name.lower().endswith(".csv"):
                df = pd.read_csv(uploaded)
                buf = io.BytesIO()
                df.to_excel(buf, index=False)
                buf.seek(0)
                inserted = load_inventory_from_excel(buf)
            else:
                inserted = load_inventory_from_excel(uploaded)
            st.success(f"Sukses memuat {inserted} baris dari file ke inventaris")
        except Exception as e:
            st.error("Gagal memuat file: " + str(e))
    st.markdown("---")
    st.subheader("Lihat Inventaris Saat Ini")
    st.dataframe(get_inventory_df())

# --- Barang Masuk ---
elif menu == "Barang Masuk":
    st.title("Form Barang Masuk")
    mode = st.radio("Mode input", ["Single-item","Multi-item"])
    items_list = get_items_list()

    if mode == "Single-item":
        with st.form("in_single"):
            choose_existing = st.checkbox("Pilih dari daftar item yang ada", value=True)
            if choose_existing and items_list:
                name_sel = st.selectbox("Nama barang", ["-- (pilih) --"]+items_list)
                if name_sel != "-- (pilih) --":
                    name = name_sel
                    unit = get_item_unit(name)
                else:
                    name = st.text_input("Nama barang baru")
                    unit = st.text_input("Satuan")
            else:
                name = st.text_input("Nama barang baru")
                unit = st.text_input("Satuan")
            qty = st.number_input("Jumlah", min_value=0.0, value=0.0)
            category = st.text_input("Kategori (opsional)")
            min_stock = st.number_input("Min stok (opsional)", min_value=0.0, value=0.0)
            supplier = st.text_input("Nama pemasok (opsional)")
            rack_location = st.text_input("Rak Penempatan (opsional)")
            expiry_date = st.date_input("Tanggal Kadaluarsa (opsional)", value=None)
            submitted = st.form_submit_button("Simpan Barang Masuk")
            if submitted:
                if not name or qty <= 0 or not unit:
                    st.error("Nama, jumlah (>0), dan satuan harus diisi")
                else:
                    item_id = upsert_item(name, category, unit, qty, min_stock, rack_location, expiry_date)
                    trx_code = generate_trx_code("in")
                    add_transaction_record("in", item_id, name, qty, unit, requester=None, supplier=supplier, note="Single-item masuk", bundle_code=trx_code, trx_code=trx_code, expiry_date=expiry_date)
                    st.success(f"Sukses: {qty} {unit} {name} ditambahkan. Trx: {trx_code}")
                    st.created_atrerun()

    else:
        # Multi-item
        if "in_multi" not in st.session_state:
            st.session_state.in_multi = []
        if st.button("Tambah Item"):
            st.session_state.in_multi.append({"name":"","unit":"","quantity":0.0,"category":"","min_stock":0.0,"rack_location":"","expiry_date":""})
        with st.form("in_multi_form"):
            supplier = st.text_input("Nama pemasok")
            note = st.text_area("Catatan transaksi (opsional)")
            for i, it in enumerate(st.session_state.in_multi):
                st.markdown(f"**Item #{i+1}**")
                cols = st.columns([3,1,1,1,1,1,1])
                name_choice = cols[0].selectbox(f"Nama {i+1}", options=["-- (new/pilih) --"]+items_list, key=f"in_name_sel_{i}")
                if name_choice != "-- (new/pilih) --":
                    name = name_choice
                    unit = get_item_unit(name)
                    cols[1].text_input("Satuan", value=unit, key=f"in_unit_{i}")
                else:
                    name = cols[0].text_input("Nama barang", value=it.get("name",""), key=f"in_name_{i}")
                    unit = cols[1].text_input("Satuan", value=it.get("unit",""), key=f"in_unit_new_{i}")
                qty = cols[2].number_input("Jumlah", min_value=0.0, value=float(it.get("quantity",0.0)), key=f"in_qty_{i}")
                min_s = cols[3].number_input("Min stok", min_value=0.0, value=float(it.get("min_stock",0.0)), key=f"in_min_{i}")
                rack = cols[4].text_input("Rak", value=it.get("rack_location",""), key=f"in_rack_{i}")
                expiry = cols[5].text_input("Kadaluarsa", value=it.get("expiry_date",""), key=f"in_exp_{i}")
                remove = cols[6].button("Hapus", key=f"in_del_{i}")
                if remove:
                    st.session_state.in_multi.pop(i)
                    st.created_atrerun()
                st.session_state.in_multi[i] = {"name": name, "unit": unit, "quantity": qty, "category": it.get("category",""), "min_stock": min_s, "rack_location": rack, "expiry_date": expiry}
            submitted = st.form_submit_button("Simpan Transaksi Masuk (Batch)")
            if submitted:
                if not st.session_state.in_multi:
                    st.error("Tidak ada item untuk disimpan")
                else:
                    errors = []
                    for idx, it in enumerate(st.session_state.in_multi):
                        if not it["name"] or it["quantity"] <= 0 or not it["unit"]:
                            errors.append(f"Baris {idx+1}: Nama, satuan, dan jumlah (>0) harus diisi")
                    if errors:
                        st.error("\n".join(errors))
                    else:
                        trx_code = generate_trx_code("in")
                        bundle = trx_code
                        for it in st.session_state.in_multi:
                            # convert expiry if string to date if possible
                            exp = None
                            if it.get("expiry_date"):
                                try:
                                    exp = pd.to_datetime(it.get("expiry_date")).date()
                                except:
                                    exp = None
                            item_id = upsert_item(it["name"], it.get("category",""), it["unit"], it["quantity"], it.get("min_stock",0.0), it.get("rack_location",""), exp)
                            add_transaction_record("in", item_id, it["name"], it["quantity"], it["unit"], requester=None, supplier=supplier, note=note, bundle_code=bundle, trx_code=trx_code, expiry_date=exp)
                        st.success(f"Sukses menyimpan batch masuk. Trx: {trx_code}")
                        st.session_state.in_multi = []
                        st.created_atrerun()

# --- Barang Keluar ---
elif menu == "Barang Keluar":
    st.title("Form Barang Keluar")
    mode = st.radio("Mode input", ["Single-item","Multi-item"], key="out_mode")
    items_list = get_items_list()

    if mode == "Single-item":
        with st.form("out_single"):
            choose_existing = st.checkbox("Pilih dari daftar item yang ada", value=True)
            if choose_existing and items_list:
                name = st.selectbox("Nama barang", options=["-- (pilih) --"]+items_list)
                if name == "-- (pilih) --":
                    name = ""
                unit = get_item_unit(name) if name else st.text_input("Satuan (baru)")
            else:
                name = st.text_input("Nama barang")
                unit = st.text_input("Satuan")
            qty = st.number_input("Jumlah", min_value=0.0, value=0.0)
            requester = st.text_input("Nama peminta")
            note = st.text_input("Keterangan (opsional)")
            submitted = st.form_submit_button("Simpan Barang Keluar")
            if submitted:
                if not name or qty <= 0 or not unit or not requester:
                    st.error("Nama, jumlah (>0), satuan dan nama peminta harus diisi")
                else:
                    # validate stock
                    res = supabase.table("items").select("quantity").eq("name", name).eq("unit", unit).limit(1).execute()
                    if not res.data:
                        st.error("Item tidak ditemukan di inventory")
                    else:
                        if (res.data[0].get("quantity") or 0) < qty:
                            st.error(f"Stok tidak cukup. Stok: {res.data[0].get('quantity')}, diminta: {qty}")
                        else:
                            item_id, err = adjust_item_for_out(name, unit, qty)
                            if err:
                                st.error(err)
                            else:
                                trx_code = generate_trx_code("out")
                                add_transaction_record("out", item_id, name, qty, unit, requester=requester, supplier=None, note=note, bundle_code=trx_code, trx_code=trx_code)
                                st.success(f"Sukses: {qty} {unit} {name} dikeluarkan. Trx: {trx_code}")
                                st.created_atrerun()
    else:
        # multi-item keluar
        if "out_multi" not in st.session_state:
            st.session_state.out_multi = []
        if st.button("Tambah Item Keluar"):
            st.session_state.out_multi.append({"name":"","unit":"","quantity":0.0,"note":""})
        with st.form("out_multi_form"):
            requester = st.text_input("Nama peminta")
            note_all = st.text_area("Catatan transaksi (opsional)")
            for i, it in enumerate(st.session_state.out_multi):
                st.markdown(f"**Item #{i+1}**")
                cols = st.columns([3,1,1,2])
                name_choice = cols[0].selectbox(f"Nama barang {i+1}", options=["-- (pilih/new) --"]+items_list, key=f"out_name_sel_{i}")
                if name_choice != "-- (pilih/new) --":
                    name = name_choice
                    unit = get_item_unit(name)
                    cols[1].text_input("Satuan", value=unit, key=f"out_unit_{i}")
                else:
                    name = cols[0].text_input("Nama barang", value=it.get("name",""), key=f"out_name_{i}")
                    unit = cols[1].text_input("Satuan", value=it.get("unit",""), key=f"out_unit_new_{i}")
                qty = cols[2].number_input("Jumlah", min_value=0.0, value=float(it.get("quantity",0.0)), key=f"out_qty_{i}")
                note_item = cols[3].text_input("Keterangan", value=it.get("note",""), key=f"out_note_{i}")
                remove = cols[3].button("Hapus", key=f"out_del_{i}")
                if remove:
                    st.session_state.out_multi.pop(i)
                    st.created_atrerun()
                st.session_state.out_multi[i] = {"name": name, "unit": unit, "quantity": qty, "note": note_item}
            submitted = st.form_submit_button("Simpan Transaksi Keluar (Batch)")
            if submitted:
                if not st.session_state.out_multi:
                    st.error("Tidak ada item untuk disimpan")
                elif not requester:
                    st.error("Nama peminta harus diisi")
                else:
                    bad = []
                    for idx, it in enumerate(st.session_state.out_multi):
                        if not it["name"] or it["quantity"] <= 0 or not it["unit"]:
                            bad.append(f"Baris {idx+1}: Nama, satuan dan jumlah (>0) harus diisi")
                    if bad:
                        st.error("\n".join(bad))
                    else:
                        # check all stocks first
                        insufficient = []
                        for it in st.session_state.out_multi:
                            res = supabase.table("items").select("quantity").eq("name", it["name"]).eq("unit", it["unit"]).limit(1).execute()
                            if not res.data:
                                insufficient.append((it["name"], "Item tidak ditemukan"))
                            else:
                                if (res.data[0].get("quantity") or 0) < it["quantity"]:
                                    insufficient.append((it["name"], f"Stok: {res.data[0].get('quantity')}, diminta: {it['quantity']}"))
                        if insufficient:
                            msgs = [f"{n}: {m}" for n,m in insufficient]
                            st.error("Transaksi ditolak karena stok tidak mencukupi atau item hilang:\n" + "\n".join(msgs))
                        else:
                            trx_code = generate_trx_code("out")
                            bundle = trx_code
                            for it in st.session_state.out_multi:
                                item_id, _ = adjust_item_for_out(it["name"], it["unit"], it["quantity"])
                                add_transaction_record("out", item_id, it["name"], it["quantity"], it["unit"], requester=requester, supplier=None, note=it.get("note",""), bundle_code=bundle, trx_code=trx_code)
                            st.success(f"Sukses menyimpan batch keluar. Trx: {trx_code}")
                            st.session_state.out_multi = []
                            st.created_atrerun()

    st.markdown("---")
    st.subheader("Inventaris Saat Ini")
    st.dataframe(get_inventory_df())

# --- Laporan & Analisis ---
elif menu == "Laporan & Analisis":
    st.title("Laporan & Analisis")
    df = load_transactions_df()
    st.subheader("Filter")
    col1, col2, col3 = st.columns(3)
    with col1:
        period = st.selectbox("Periode", ["Mingguan","Bulanan"])
    with col2:
        date_from = st.date_input("Dari", value=(datetime.now().date() - timedelta(days=30)))
    with col3:
        date_to = st.date_input("Sampai", value=datetime.now().date())
    date_from = pd.to_datetime(date_from)
    date_to = pd.to_datetime(date_to)
    st.markdown("---")
    if df.empty:
        st.info("Belum ada transaksi untuk ditampilkan")
    else:
        totals = totals_for_period(df, date_from=date_from, date_to=date_to)
        st.subheader("Total per Item dalam Periode Terpilih")
        st.dataframe(totals)
        in_period = df[(df["trx_type"]=="in") & (df["date"]>=date_from) & (df["date"]<=date_to)]
        out_period = df[(df["trx_type"]=="out") & (df["date"]>=date_from) & (df["date"]<=date_to)]
        st.subheader("Transaksi Masuk (IN)")
        if in_period.empty:
            st.info("Tidak ada transaksi masuk pada periode yang dipilih")
        else:
            st.dataframe(in_period)
            st.markdown("Grafik: Total Masuk per Item (periode terpilih)")
            in_sum = in_period.groupby("name")["quantity"].sum().reset_index()
            st.altair_chart(alt.Chart(in_sum).mark_bar().encode(x="name:N", y="quantity:Q").properties(height=300), use_container_width=True)
        st.subheader("Transaksi Keluar (OUT)")
        if out_period.empty:
            st.info("Tidak ada transaksi keluar pada periode yang dipilih")
        else:
            st.dataframe(out_period)
            out_sum = out_period.groupby("name")["quantity"].sum().reset_index()
            st.altair_chart(alt.Chart(out_sum).mark_bar().encode(x="name:N", y="quantity:Q").properties(height=300), use_container_width=True)

        # Monthly/Weekly summary
        if period == "Mingguan":
            g = df.copy()
            g["week"] = g["created_at"].dt.strftime("%Y-%W")
            pivot = g.groupby(["week","name","unit","trx_type"])["quantity"].sum().reset_index()
            if pivot.empty:
                st.info("Tidak ada data mingguan")
            else:
                st.dataframe(pivot)
        else:
            g = df.copy()
            g["month"] = g["created_at"].dt.strftime("%Y-%m")
            pivot = g.groupby(["month","name","unit","trx_type"])["quantity"].sum().reset_index()
            if pivot.empty:
                st.info("Tidak ada data bulanan")
            else:
                st.dataframe(pivot)

    st.markdown("---")
    st.subheader("Download Data")
    if st.button("Download seluruh DB (Excel)"):
        bytes_xlsx = export_db_to_excel_bytes()
        st.download_button("Klik untuk download seluruh DB", bytes_xlsx, file_name="gudang_supabase.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# --- Pengaturan ---
elif menu == "Pengaturan":
    st.title("Pengaturan")
    st.subheader("Manajemen User (Sederhana)")
    with st.form("form_user"):
        new_user = st.text_input("Username baru")
        new_pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Tambah user")
        if submitted:
            if not new_user or not new_pw:
                st.error("Isi username dan password")
            else:
                try:
                    supabase.table("users").insert({"username": new_user, "password_hash": hash_pw(new_pw)}).execute()
                    st.success("User ditambahkan")
                except Exception as e:
                    st.error("Gagal menambah user: " + str(e))
    st.markdown("---")
    st.subheader("Hapus / Reset DB (HATI-HATI)")
    if st.checkbox("Tunjukkan opsi reset DB"):
        if st.button("Reset seluruh DB (hapus semua records)"):
            # Hati-hati: hanya menghapus isi tabel, tidak menjatuhkan struktur.
            supabase.table("transactions").delete().neq("id", -1).execute()
            supabase.table("items").delete().neq("id", -1).execute()
            supabase.table("users").delete().neq("username", "keep_admin").execute()  # contoh: mengosongkan users
            st.success("DB telah dikosongkan. Silakan refresh.")
