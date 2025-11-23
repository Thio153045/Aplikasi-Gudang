"""
Streamlit Warehouse app (Gudang Bahan Makanan & Alat Kerja)
Single-file app: streamlit_gudang_app.py
Requirements:
  pip install streamlit pandas openpyxl altair

Features implemented (extended):
  - Login (username/password) with simple sqlite users table (default admin/admin123)
  - Upload initial inventory from Excel
  - Single-item & Multi-item forms for Barang Masuk & Barang Keluar (with explicit Save/Submit buttons)
  - Dynamic rows (+ Tambah Item, Hapus baris) for multi-item
  - SQLite backend: items, transactions, users; transactions now have `bundle_code` to group multi-item transactions
  - Auto-fill unit when selecting existing item from dropdown
  - Searchable dropdowns (Streamlit selectbox supports typing)
  - Auto-generate transaction code: TRX-[IN/OUT]-YYYYMMDD-HHMMSS
  - Validation: Barang Keluar rejected if any item stock insufficient
  - Totals per item in Dashboard and Reports; Weekly/Monthly summaries; Comparison between months
  - Export: entire DB or specific reports to Excel

Notes:
 - This is a single-file reference implementation. You can refine UI/UX and security (password hashing/salting, sessions) further.
 - To run: `streamlit run streamlit_gudang_app.py`
"""

import streamlit as st
import pandas as pd
import sqlite3
import hashlib
import io
from datetime import datetime, timedelta
import altair as alt
import os
import random
from contextlib import closing

DB_PATH = "gudang.db"

# ------------------- Utilities -------------------

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT,
                unit TEXT,
                quantity REAL DEFAULT 0,
                min_stock REAL DEFAULT 0,
                rack_location TEXT,
                expiry_date TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trx_type TEXT NOT NULL, -- 'in' or 'out'
                item_id INTEGER,
                name TEXT,
                quantity REAL,
                unit TEXT,
                requester TEXT,
                supplier TEXT,
                note TEXT,
                created_at TEXT,
                bundle_code TEXT,
                trx_code TEXT,
                expiry_date TEXT 
                
            )
            """
        )
        conn.commit()
        


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def ensure_default_admin():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        cnt = cur.fetchone()[0]
        if cnt == 0:
            cur.execute(
                "INSERT INTO users(username, password_hash) VALUES (?, ?)",
                ("admin", hash_pw("admin123")),
            )
            conn.commit()


def verify_login(username, password):
    pw_hash = hash_pw(password)
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE username=?", (username,))
        row = cur.fetchone()
        if not row:
            return False
        return row[0] == pw_hash


# ------------------- Helpers -------------------

def generate_trx_code(trx_type):
    now = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"TRX-{trx_type.upper()}-{now}-{random.randint(100,999)}"


def upsert_item(name, category, unit, quantity, min_stock=0, rack_location="", expiry_date=None):

    name = name.strip()
    now = datetime.now().isoformat()

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # cek apakah item sudah ada
        cur.execute(
            "SELECT id, quantity FROM items WHERE name=? AND unit=?",
            (name, unit)
        )
        row = cur.fetchone()

        if row:
            item_id, existing_quantity = row
            new_quantity = existing_quantity + quantity

            # update item existing
            cur.execute(
                """
                UPDATE items SET  quantity=?, category=?, min_stock=?, rack_location=?, expiry_date=?, created_at=?, updated_at=?
                WHERE id=?

                """,
                (new_quantity, category, min_stock, rack_location,expiry_date, now,now, item_id)
            )
            conn.commit()
            return item_id

        else:
            # insert item baru
            cur.execute(
                """
                INSERT INTO items(name, category, unit, quantity, min_stock, rack_location, expiry_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, category, unit, quantity, min_stock, rack_location, expiry_date, now, now)
            )
            conn.commit()
            return cur.lastrowid



def adjust_item_for_out(name, unit, quantity):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, quantity FROM items WHERE name=? AND unit=?", (name, unit))
        row = cur.fetchone()
        if not row:
            return None, "Item tidak ditemukan"
        item_id, existing_quantity = row
        if existing_quantity < quantity:
            return None, "Stok tidak cukup: tersedia {}".format(existing_quantity)
        new_quantity = existing_quantity - quantity
        cur.execute("UPDATE items SET quantity=? WHERE id=?", (new_quantity, item_id))
        conn.commit()
        return item_id, None


def add_transaction_record(trx_type, item_id, name, quantity, unit, requester, supplier, note, bundle_code, trx_code, expiry_date=None):
    now = datetime.now()
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO transactions(
                trx_type, item_id, name, quantity, unit, requester, supplier, note,
                bundle_code, trx_code, expiry_date, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trx_type,
            item_id,
            name,
            quantity,
            unit,
            requester,
            supplier,
            note,
            bundle_code,
            trx_code,
            expiry_date,
            now
        ))

        conn.commit()



# ------------------- Loading / Exporting -------------------

def load_inventory_from_excel(buffer):
    df = pd.read_excel(buffer)
    df_columns = {c.lower(): c for c in df.columns}

    required = ['name', 'quantity', 'unit']
    for r in required:
        if r not in df_columns:
            raise ValueError(f"Excel harus memiliki kolom: {', '.join(required)}")

    inserted = 0
    for _, row in df.iterrows():
        name = str(row[df_columns['name']]).strip()
        quantity = float(row[df_columns['quantity']]) if not pd.isna(row[df_columns['quantity']]) else 0
        unit = str(row[df_columns['unit']]).strip()
        category = str(row[df_columns['category']]).strip() if 'category' in df_columns else ''
        min_stock = float(row[df_columns['min_stock']]) if 'min_stock' in df_columns and not pd.isna(row[df_columns['min_stock']]) else 0

        # 🔥 Tambahan paling penting
        rack_location = str(row[df_columns['rack_location']]).strip() if 'rack_location' in df_columns else ''
        expiry_date = (
            row[df_columns['expiry_date']].date() 
            if 'expiry_date' in df_columns and not pd.isna(row[df_columns['expiry_date']])
            else None
        )

        # Anda harus update fungsi upsert agar menerima parameter baru:
        upsert_item(name, category, unit, quantity, min_stock, rack_location, expiry_date)

        inserted += 1

    return inserted



def export_db_to_excel():
    with closing(get_conn()) as conn:
        items = pd.read_sql_query("SELECT * FROM items", conn)
        trans = pd.read_sql_query("SELECT * FROM transactions ORDER BY created_at DESC", conn)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        items.to_excel(writer, sheet_name='inventory', index=False)
        trans.to_excel(writer, sheet_name='transactions', index=False)
        writer.close()
    processed_data = output.getvalue()
    return processed_data


def export_df_to_excel_bytes(dict_of_dfs: dict):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, df in dict_of_dfs.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        writer.close()
    return output.getvalue()


# ------------------- Reporting -------------------

def load_transactions_df():
    with closing(get_conn()) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM transactions",
            conn,
            parse_dates=['created_at']   # pastikan kolom ini ada di DB
        )

    if df.empty:
        return df

    # pastikan format datetime benar
    df['created_at'] = pd.to_datetime(df['created_at'])

    # tambahan kolom tanggal, bulan, minggu
    df['date'] = df['created_at'].dt.date
    df['month'] = df['created_at'].dt.to_period('M').dt.to_timestamp()
    df['week'] = df['created_at'].dt.to_period('W').dt.start_time

    return df



def summary_by_period(df, period='W'):
    if df.empty:
        return pd.DataFrame()

    # pastikan df['date'] berbentuk datetime
    df['date'] = pd.to_datetime(df['date'])

    if period == 'W':
        df['week'] = df['date'].dt.strftime('%Y-%W')
        g = df.groupby(['week', 'name', 'unit', 'trx_type'])['quantity'].sum().reset_index()
        return g

    if period == 'M':
        df['month'] = df['date'].dt.strftime('%Y-%m')
        g = df.groupby(['month', 'name', 'unit', 'trx_type'])['quantity'].sum().reset_index()
        return g

    return pd.DataFrame()


def totals_for_period(df, date_from=None, date_to=None):
    # baca stok akhir
    with closing(get_conn()) as conn:
        items_df = pd.read_sql_query(
            "SELECT id, name, unit, quantity as current_quantity FROM items", conn
        )

    # jika df kosong
    if df.empty:
        res = items_df.rename(columns={'name':'name','current_quantity':'stok_akhir'})
        res['total_masuk'] = 0
        res['total_keluar'] = 0
        return res[['name','unit','total_masuk','total_keluar','stok_akhir']]

    # pastikan tanggal berbentuk datetime Pandas
    df['date'] = pd.to_datetime(df['date'])

    if date_from:
        date_from = pd.to_datetime(date_from)

    if date_to:
        date_to = pd.to_datetime(date_to)

    # filter
    mask = pd.Series([True] * len(df))

    if date_from:
        mask &= (df['date'] >= date_from)

    if date_to:
        mask &= (df['date'] <= date_to)

    period_df = df[mask]

    # hitung masuk
    masuk = (
        period_df[period_df['trx_type']=='in']
        .groupby(['name','unit'])['quantity']
        .sum()
        .reset_index()
        .rename(columns={'quantity':'total_masuk'})
    )

    # hitung keluar
    keluar = (
        period_df[period_df['trx_type']=='out']
        .groupby(['name','unit'])['quantity']
        .sum()
        .reset_index()
        .rename(columns={'quantity':'total_keluar'})
    )

    # merge
    merged = pd.merge(masuk, keluar, on=['name','unit'], how='outer').fillna(0)

    merged = pd.merge(
        merged,
        items_df.rename(columns={'name':'name','current_quantity':'stok_akhir'}),
        on=['name','unit'],
        how='right'
    )

    # isi kosong
    merged['total_masuk'] = merged['total_masuk'].fillna(0)
    merged['total_keluar'] = merged['total_keluar'].fillna(0)
    merged['stok_akhir'] = merged['stok_akhir'].fillna(0)

    return merged[['name','unit','total_masuk','total_keluar','stok_akhir']]


def compare_months(df, month_a, month_b, items_list=None):
    df_month = df.copy()
    df_month['month_start'] = df_month['created_at'].dt.to_period('M').dt.to_created_at()
    sel = df_month[df_month['month_start'].isin([month_a, month_b]) & (df_month['trx_type']=='out')]
    if items_list:
        sel = sel[sel['name'].isin(items_list)]
    pivot = sel.groupby(['month_start','name'])['quantity'].sum().reset_index()
    cmp = pivot.pivot_table(index='name', columns='month_start', values='quantity', aggfunc='sum').fillna(0)
    if month_a in cmp.columns and month_b in cmp.columns:
        cmp['difference'] = cmp[month_b] - cmp[month_a]
        cmp['pct_change'] = cmp.apply(lambda r: (r['difference']/r[month_a]*100) if r[month_a]!=0 else None, axis=1)
    return cmp.reset_index()


# ------------------- Streamlit App -------------------

st.set_page_config(page_title='Gudang - Streamlit', layout='wide')

init_db()
ensure_default_admin()

if 'auth' not in st.session_state:
    st.session_state.auth = False
if 'user' not in st.session_state:
    st.session_state.user = None

# --- Login ---
if not st.session_state.auth:
    st.title("Login - Aplikasi Gudang")
    with st.form('login_form'):
        username = st.text_input('Username')
        password = st.text_input('Password', type='password')
        submitted = st.form_submit_button('Login')
        if submitted:
            if verify_login(username, password):
                st.session_state.auth = True
                st.session_state.user = username
                st.success('Login sukses sebagai ' + username)
                st.rerun()
            else:
                st.error('Username atau password salah')

    st.markdown("---")
    st.info("Default akun: **admin / admin123** (ubah segera setelah masuk)")
    st.stop()

# --- Main app after login ---
st.sidebar.title('Menu')
menu = st.sidebar.radio('Pilih', ['Dashboard', 'Upload Inventaris (Excel)', 'Barang Masuk', 'Barang Keluar', 'Laporan & Analisis', 'Pengaturan'])

st.sidebar.write('User:', st.session_state.user)
if st.sidebar.button('Logout'):
    st.session_state.auth = False
    st.session_state.user = None
    st.rerun()

# Helpers to display inventory

def get_inventory_df():
    with closing(get_conn()) as conn:
        df = pd.read_sql_query("SELECT * FROM items ORDER BY name", conn)
    return df


def get_items_list():
    df = get_inventory_df()
    if df.empty:
        return []
    return df['name'].tolist()


def get_item_unit(name):
    if not name:
        return ''
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT unit FROM items WHERE name=? LIMIT 1", (name,))
        row = cur.fetchone()
        return row[0] if row else ''

# Dashboard
if menu == 'Dashboard':
    st.title('Dashboard Gudang')
    inv = get_inventory_df()
    st.subheader('Ringkasan Stok')
    if inv.empty:
        st.info('Inventaris kosong. Silakan upload data awal atau tambah barang masuk.')
    else:
        st.dataframe(inv)
        low = inv[inv['quantity'] <= inv['min_stock']]
        if not low.empty:
            st.warning('Beberapa item mencapai atau di bawah minimal stok:')
            st.table(low[['name', 'quantity', 'min_stock', 'unit']])

    st.subheader('Transaksi Terakhir')
    with closing(get_conn()) as conn:
        trans = pd.read_sql_query("SELECT * FROM transactions ORDER BY created_at DESC LIMIT 20", conn)
    st.dataframe(trans)

    # Total per item summary (overall)
    st.subheader('Total per Item (seluruh waktu)')
    df_all = load_transactions_df()
    totals_all = totals_for_period(df_all)
    st.dataframe(totals_all)
    st.markdown('Grafik Pemakaian (Total Keluar per Item - seluruh waktu)')
    if not df_all.empty:
        out_all = df_all[df_all['trx_type']=='out'].groupby(['name'])['quantity'].sum().reset_index()
        chart = alt.Chart(out_all).mark_bar().encode(x='name:N', y='quantity:Q').properties(height=300).interactive()
        st.altair_chart(chart, use_container_width=True)

# Upload inventory
elif menu == 'Upload Inventaris (Excel)':
    st.title('Upload Inventaris Awal dari Excel')
    st.markdown('Format yang disarankan: kolom `name`, `quantity`, `unit`, optional `category`, `min_stock`')
    uploaded = st.file_uploader('Pilih file Excel (.xlsx) atau CSV', type=['xlsx', 'xls', 'csv'])
    if uploaded:
        try:
            if uploaded.name.lower().endswith('.csv'):
                df = pd.read_csv(uploaded)
                buffer = io.BytesIO()
                df.to_excel(buffer, index=False)
                buffer.seek(0)
                inserted = load_inventory_from_excel(buffer)
            else:
                inserted = load_inventory_from_excel(uploaded)
            st.success(f'Sukses memuat {inserted} baris dari file ke inventaris')
        except Exception as e:
            st.error('Gagal memuat file: ' + str(e))
    st.markdown('---')
    st.subheader('Lihat Inventaris Saat Ini')
    st.dataframe(get_inventory_df())

# Barang Masuk
elif menu == 'Barang Masuk':
    st.title('Form Barang Masuk')
    st.write('Ada dua mode: Single-item atau Multi-item. Pilih mode di bawah.')
    mode = st.radio('Mode input', ['Single-item','Multi-item'])

    items_list = get_items_list()

    # --- Single-item ---
    if mode == 'Single-item':
        st.subheader('Single-item (cepat)')
        with st.form('in_single'):
            use_existing = st.checkbox('Pilih dari daftar item yang ada', value=True)
            if use_existing and items_list:
                name_select = st.selectbox('Nama barang', ['-- (pilih) --'] + items_list)
                if name_select != '-- (pilih) --':
                    name = name_select
                    unit = get_item_unit(name)
                else:
                    name = st.text_input("Nama barang baru")
                    unit = st.text_input("Satuan")
            else:
                name = st.text_input("Nama barang baru")
                unit = st.text_input("Satuan")
            quantity = st.number_input('Jumlah', min_value=0.0, value=0.0)
            category = st.text_input('Kategori (opsional)')
            min_stock = st.number_input('Min stok (opsional)', min_value=0.0, value=0.0)
            supplier = st.text_input('Nama pemasok (opsional)')
            rack_location = st.text_input('Rak Penempatan (opsional)')
            expiry_date=st.text_input('Tanggal Kadaluarsa')
            trx_code = generate_trx_code('in')
            submitted = st.form_submit_button('Simpan Barang Masuk')
            if submitted:
                if not name or quantity <= 0 or not unit:
                    st.error('Nama, jumlah (>0) dan satuan harus diisi')
                else:
                    item_id = upsert_item(name, category, unit, quantity, min_stock,rack_location)
                    bundle = trx_code
                    add_transaction_record('in', item_id, name, quantity, unit, requester=None, supplier=supplier, note='Single-item masuk', bundle_code=bundle, trx_code=trx_code)
                    st.success(f'Sukses: {quantity} {unit} {name} ditambahkan. Trx: {trx_code}')
                    st.rerun()

    # --- Multi-item ---
    # --- Multi-item ---
    else:
        st.subheader('Multi-item (batch)')

    # init session state
        if 'in_multi' not in st.session_state:
            st.session_state.in_multi = []

    # tombol tambah baris
        if st.button('Tambah Item'):
            st.session_state.in_multi.append({'name':'','unit':'','quantity':0.0,'category':'','min_stock':0.0})

    # ---------- FORM MULTI-ITEM ----------
        # ---------- FORM MULTI-ITEM ----------
        with st.form('in_multi_form'):
            colA, colB = st.columns([2,1])
            with colA:
                tdate = st.date_input('Tanggal transaksi', value=datetime.now().date())
                supplier = st.text_input('Nama pemasok')
                note = st.text_area('Catatan transaksi (opsional)')
            with colB:
                st.write('Baris current:', len(st.session_state.in_multi))

    # per baris
            for i, it in enumerate(st.session_state.in_multi):
                st.markdown(f'**Item #{i+1}**')

        # Tambah kolom expiry date → total 7 kolom
                cols = st.columns([3, 1, 1, 2, 1, 2, 2])

        # Nama barang
                name_choice = cols[0].selectbox(
                    f'Nama barang {i+1}',
                    options=['-- (new / pilih) --'] + items_list,
                    key=f'in_multi_name_sel_{i}'
                )

                if name_choice != '-- (new / pilih) --':
                    name = name_choice
                    unit = get_item_unit(name)
                    cols[1].text_input('Satuan', value=unit, key=f'in_multi_unit_{i}')
                else:
                    name = cols[0].text_input('Nama barang (baru)', value=it.get('name', ''), key=f'in_multi_name_{i}')
                    unit = cols[1].text_input('Satuan', value=it.get('unit', ''), key=f'in_multi_unit_{i}')

                quantity = cols[2].number_input('Jumlah', min_value=0.0, value=float(it.get('quantity', 0.0)), key=f'in_multi_quantity_{i}')
                min_stock = cols[3].number_input('Min stok', min_value=0.0, value=float(it.get('min_stock', 0.0)), key=f'in_multi_min_{i}')
                rack_location = cols[5].text_input('Rak', value=it.get('rack_location',''), key=f'in_multi_rack_{i}')

        # NEW → input expiry date per baris
                expiry_date = cols[6].text_input(
                    'Kadaluarsa',
                    value=it.get('expiry_date', ''),
                    key=f'in_multi_expiry_{i}'
                )

        # DELETE BUTTON
                del_row = cols[4].form_submit_button("🗑")
                if del_row:
                    st.session_state.in_multi.pop(i)
                    st.rerun()

        # UPDATE STATE
                st.session_state.in_multi[i] = {
                   'name': name,
                   'unit': unit,
                   'quantity': quantity,
                   'category': it.get('category',''),
                   'min_stock': min_stock,
                   'rack_location': rack_location,
                   'expiry_date': expiry_date
                }

            submitted = st.form_submit_button('Simpan Transaksi Masuk (Batch)')


    # ---------- SETELAH SUBMIT ----------
        if submitted:
            if not st.session_state.in_multi:
                st.error('Tidak ada item untuk disimpan')
            else:
            # validasi
                errors = []
                for idx, it in enumerate(st.session_state.in_multi):
                    if not it['name'] or it['quantity'] <= 0 or not it['unit']:
                        errors.append(f'Baris {idx+1}: Nama, satuan dan jumlah (>0) harus diisi')

                if errors:
                    st.error("\n".join(errors))
                else:
                    trx_code = generate_trx_code('in')
                    bundle = trx_code
                    for it in st.session_state.in_multi:
                        item_id = upsert_item(it['name'], it.get('category',''), it['unit'], it['quantity'], it.get('min_stock',0.0),it.get('rack_location'),it.get('expiry_date'))
                        add_transaction_record(
                            'in', item_id, it['name'], it['quantity'], it['unit'],
                            requester=None, supplier=supplier, note=note,
                            bundle_code=bundle, trx_code=trx_code
                        )

                    st.success(f'Sukses menyimpan batch masuk. Trx: {trx_code}')
                    st.session_state.in_multi = []
                    st.rerun()


# Barang Keluar
elif menu == 'Barang Keluar':
    st.title('Form Barang Keluar')
    st.write('Ada dua mode: Single-item atau Multi-item. Pilih mode di bawah.')
    mode = st.radio('Mode input', ['Single-item','Multi-item'], key='out_mode')

    items_list = get_items_list()

    # --- Single-item keluar ---
    if mode == 'Single-item':
        st.subheader('Single-item (cepat)')
        with st.form('out_single'):
            use_existing = st.checkbox('Pilih dari daftar item yang ada', value=True)
            if use_existing and items_list:
                name = st.selectbox('Nama barang', options=['-- (pilih) --'] + items_list)
                if name == '-- (pilih) --':
                    name = ''
                unit = get_item_unit(name) if name else st.text_input('Satuan (baru)')
            else:
                name = st.text_input('Nama barang')
                unit = st.text_input('Satuan')
            quantity = st.number_input('Jumlah', min_value=0.0, value=0.0)
            requester = st.text_input('Nama peminta')
            note = st.text_input('Keterangan (opsional)')
            trx_code = generate_trx_code('out')
            submitted = st.form_submit_button('Simpan Barang Keluar')
            if submitted:
                if not name or quantity <= 0 or not unit or not requester:
                    st.error('Nama, jumlah (>0), satuan dan nama peminta harus diisi')
                else:
                    # validate stock
                    with closing(get_conn()) as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT quantity FROM items WHERE name=? AND unit=?", (name, unit))
                        row = cur.fetchone()
                        if not row:
                            st.error('Item tidak ditemukan di inventory')
                        else:
                            if row[0] < quantity:
                                st.error(f'Stok tidak cukup. Stok: {row[0]}, diminta: {quantity}')
                            else:
                                item_id = adjust_item_for_out(name, unit, quantity)[0]
                                add_transaction_record('out', item_id, name, quantity, unit, requester=requester, supplier=None, note=note, bundle_code=trx_code, trx_code=trx_code)
                                st.success(f'Sukses: {quantity} {unit} {name} dikeluarkan. Trx: {trx_code}')
                                st.rerun()

    # --- Multi-item keluar ---
    else:
        st.subheader('Multi-item (batch)')
        if 'out_multi' not in st.session_state:
            st.session_state.out_multi = []
        if st.button('Tambah Item Keluar'):
            st.session_state.out_multi.append({'name':'','unit':'','quantity':0.0,'note':''})
        with st.form('out_multi_form'):
            colA, colB = st.columns([2,1])
            with colA:
                tdate = st.date_input('Tanggal transaksi', value=datetime.now().date())
                requester = st.text_input('Nama peminta')
                note = st.text_area('Catatan transaksi (opsional)')
            with colB:
                st.write('Transaction code akan dibuat otomatis setelah submit')
                st.write('Baris current: ', len(st.session_state.out_multi))

            for i, it in enumerate(st.session_state.out_multi):
                st.markdown(f'**Item #{i+1}**')
                cols = st.columns([3,1,1,2])
                name_choice = cols[0].selectbox(f'Nama barang {i+1}', options=['-- (pilih/new) --'] + items_list, key=f'out_multi_name_sel_{i}')
                if name_choice != '-- (pilih/new) --':
                    name = name_choice
                    unit = get_item_unit(name)
                    cols[1].text_input('Satuan', value=unit, key=f'out_multi_unit_{i}')
                else:
                    name = cols[0].text_input('Nama barang (baru)', value=it.get('name',''), key=f'out_multi_name_{i}')
                    unit = cols[1].text_input('Satuan', value=it.get('unit',''), key=f'out_multi_unit_{i}')
                quantity = cols[2].number_input('Jumlah', min_value=0.0, value=float(it.get('quantity',0.0)), key=f'out_multi_quantity_{i}')
                note_item = cols[3].text_input('Keterangan', value=it.get('note',''), key=f'out_multi_note_{i}')
                st.session_state.out_multi[i] = {'name': name, 'unit': unit, 'quantity': quantity, 'note': note_item}
                delete = cols[3].form_submit_button("Hapus", key=f'del_out_multi_{i}')
                if delete:
                    st.session_state.out_multi.pop(i)
                    st.rerun()

                

            submitted = st.form_submit_button('Simpan Transaksi Keluar (Batch)')
            if submitted:
                # validate
                if not st.session_state.out_multi:
                    st.error('Tidak ada item untuk disimpan')
                elif not requester:
                    st.error('Nama peminta harus diisi')
                else:
                    bad = []
                    for idx, it in enumerate(st.session_state.out_multi):
                        if not it['name'] or it['quantity'] <= 0 or not it['unit']:
                            bad.append(f'Baris {idx+1}: Nama, satuan dan jumlah (>0) harus diisi')
                    if bad:
                        st.error('\n'.join(bad))
                    else:
                        # Check stock for all items first; reject all if any insufficient
                        insufficient = []
                        with closing(get_conn()) as conn:
                            cur = conn.cursor()
                            for it in st.session_state.out_multi:
                                cur.execute("SELECT quantity FROM items WHERE name=? AND unit=?", (it['name'], it['unit']))
                                row = cur.fetchone()
                                if not row:
                                    insufficient.append((it['name'], 'Item tidak ditemukan'))
                                else:
                                    if row[0] < it['quantity']:
                                        insufficient.append((it['name'], f"Stok: {row[0]}, diminta: {it['quantity']}"))
                        if insufficient:
                            msgs = [f"{n}: {m}" for n,m in insufficient]
                            st.error('Transaksi ditolak karena stok tidak mencukupi atau item hilang:\n' + '\n'.join(msgs))
                        else:
                            trx_code = generate_trx_code('out')
                            bundle = trx_code
                            # All good: perform adjustments and records
                            for it in st.session_state.out_multi:
                                item_id, _ = adjust_item_for_out(it['name'], it['unit'], it['quantity'])
                                add_transaction_record('out', item_id, it['name'], it['quantity'], it['unit'], requester=requester, supplier=None, note=it.get('note',''), bundle_code=bundle, trx_code=trx_code)
                            st.success(f'Sukses menyimpan batch keluar. Trx: {trx_code}')
                            st.session_state.out_multi = []
                            st.rerun()

    st.markdown('---')
    st.subheader('Inventaris Saat Ini')
    st.dataframe(get_inventory_df())

# Laporan & Analisis
elif menu == 'Laporan & Analisis':
    st.title('Laporan & Analisis')
    df = load_transactions_df()
    inv = get_inventory_df()

    st.subheader('Filter')
    col1, col2, col3 = st.columns(3)
    with col1:
        period = st.selectbox('Periode', ['Mingguan', 'Bulanan'])
    with col2:
        date_from = st.date_input('Dari', value=(datetime.now().date() - timedelta(days=30)))
    with col3:
        date_to = st.date_input('Sampai', value=datetime.now().date())
    date_from = pd.to_datetime(date_from)
    date_to = pd.to_datetime(date_to)
    st.markdown('---')
    if df.empty:
        st.info('Belum ada transaksi untuk ditampilkan')
    else:
        st.subheader('Total per Item dalam Periode Terpilih')
        totals = totals_for_period(df, date_from=date_from, date_to=date_to)
        st.dataframe(totals)
                       
           
        in_period = df[(df['trx_type']=='in') & (df['date']>=date_from) & (df['date']<=date_to)]
        out_period = df[(df['trx_type']=='out') & (df['date']>=date_from) & (df['date']<=date_to)]
        st.subheader("Transaksi Masuk (IN)")
        if in_period.empty:
            st.info('Tidak ada transaksi masuk pada periode yang dipilih')
        else:
            
            st.dataframe(in_period)
            st.markdown('Grafik: Total Masuk per Item (periode terpilih)')
            in_sum = in_period.groupby('name')['quantity'].sum().reset_index()
            chart = alt.Chart(in_sum).mark_bar().encode(x='name:N', y='quantity:Q').properties(height=300).interactive()
            st.altair_chart(chart, use_container_width=True)
       
        
        st.subheader("Transaksi Keluar (OUT)")
        if out_period.empty:
            st.info('Tidak ada transaksi keluar pada periode yang dipilih')
        else:
            
            st.dataframe(out_period)
            st.markdown('Grafik: Total Keluar per Item (periode terpilih)')
            out_sum = out_period.groupby('name')['quantity'].sum().reset_index()
            chart = alt.Chart(out_sum).mark_bar().encode(x='name:N', y='quantity:Q').properties(height=300).interactive()
            st.altair_chart(chart, use_container_width=True)    
        st.markdown('---')
        
        
        if period == 'Mingguan':
            st.subheader('Ringkasan per Minggu (Total per Item)')
            g = summary_by_period(df, period='W')
            if g.empty:
                st.info('Tidak ada data mingguan')
            else:
                trx_choice = st.selectbox("Pilih transaksi", ["in", "out"])
                pivot = g.pivot_table(index=['week','name','unit'], columns='trx_type', values='quantity', aggfunc='sum').fillna(0).reset_index()
                if 'in' not in pivot.columns:
                    pivot['in'] = 0
                if 'out' not in pivot.columns:
                    pivot['out'] = 0
                pivot = pivot.rename(columns={'in':'total_masuk','out':'total_keluar'})
                st.dataframe(pivot)
                week_choice = st.selectbox('Pilih minggu (start date)', sorted(pivot['week'].unique()), index=0)
                pick = pivot[pivot['week']==week_choice]
                if not pick.empty:
                    show_col = 'total_masuk' if trx_choice=='in' else 'total_keluar'

                    st.markdown(f"Grafik: Total {trx_choice} per Item pada minggu terpilih")

                    chart2 = alt.Chart(pick).mark_bar().encode(
                        x='name:N',
                        y=f'{show_col}:Q'
                    ).properties(height=300).interactive()

                    st.altair_chart(chart2, use_container_width=True)

        else:
            st.subheader('Ringkasan per Bulan (Total per Item)')
            g = summary_by_period(df, period='M')
            if g.empty:
                st.info('Tidak ada data bulanan')
            else:
                trx_choice = st.selectbox("Pilih transaksi", ["in", "out"])
                pivot = g.pivot_table(index=['month','name','unit'], columns='trx_type', values='quantity', aggfunc='sum').fillna(0).reset_index()
                if 'in' not in pivot.columns:
                    pivot['in'] = 0
                if 'out' not in pivot.columns:
                    pivot['out'] = 0
                pivot = pivot.rename(columns={'in':'total_masuk','out':'total_keluar'})
                st.dataframe(pivot)
                month_choice = st.selectbox('Pilih bulan', sorted(pivot['month'].unique()), index=0)
                pick = pivot[pivot['month']==month_choice]
                if not pick.empty:
                    show_col = 'total_masuk' if trx_choice=='in' else 'total_keluar'

                    st.markdown(f"Grafik: Total {trx_choice} per Item pada minggu terpilih")

                    chart2 = alt.Chart(pick).mark_bar().encode(
                        x='name:N',
                        y=f'{show_col}:Q'
                    ).properties(height=300).interactive()

                    st.altair_chart(chart2, use_container_width=True)

        st.markdown('---')
        st.subheader('Perbandingan Bulanan (pilih dua bulan)')
        months = sorted(df['month'].dropna().unique())
        if len(months) < 2:
            st.info('Butuh minimal 2 bulan data untuk perbandingan')
        else:
            colA, colB = st.columns(2)
            with colA:
                m1 = st.selectbox('Bulan A', months, index=max(0, len(months)-2))
            with colB:
                m2 = st.selectbox('Bulan B', months, index=max(0, len(months)-1))
            items = sorted(df['name'].unique())
            sel_items = st.multiselect('Pilih item untuk bandingkan', items, default=items[:5])
            cmp = compare_months(df, m1, m2, items_list=sel_items if sel_items else None)
            if cmp.empty:
                st.info('Tidak ada data untuk perbandingan pada item/bulan terpilih')
            else:
                st.dataframe(cmp)
                if 'difference' in cmp.columns:
                    chart_cmp = alt.Chart(cmp).mark_bar().encode(x='name:N', y='difference:Q').properties(height=300).interactive()
                    st.altair_chart(chart_cmp, use_container_width=True)

    st.markdown('---')
    st.subheader('Download Data')
    df_all = load_transactions_df()
    totals_selected = totals_for_period(df_all, date_from=date_from, date_to=date_to)
    reports = {
        'inventory': get_inventory_df(),
        'transactions': df_all,
        'totals_period_selected': totals_selected
    }
    if st.button('Download seluruh DB (Excel)'):
        data = export_db_to_excel()
        st.download_button('Klik untuk download seluruh DB', data, file_name='gudang_export.xlsx', mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    if st.button('Download laporan (Masuk / Keluar / Totals periode)'):
        bytes_xlsx = export_df_to_excel_bytes(reports)
        st.download_button('Download laporan terpilih', bytes_xlsx, file_name='gudang_laporan.xlsx', mime='application/vnd.openxmlformats-officedocument-spreadsheetml.sheet')

# Pengaturan
elif menu == 'Pengaturan':
    st.title('Pengaturan')
    st.subheader('Manajemen User (Sederhana)')
    with st.form('form_user'):
        new_user = st.text_input('Username baru')
        new_pw = st.text_input('Password', type='password')
        submitted = st.form_submit_button('Tambah user')
        if submitted:
            if not new_user or not new_pw:
                st.error('Isi username dan password')
            else:
                with closing(get_conn()) as conn:
                    cur = conn.cursor()
                    try:
                        cur.execute("INSERT INTO users(username, password_hash) VALUES (?, ?)", (new_user, hash_pw(new_pw)))
                        conn.commit()
                        st.success('User ditambahkan')
                    except Exception as e:
                        st.error('Gagal menambah user: ' + str(e))

    st.markdown('---')
    st.subheader('Hapus / Reset DB (HATI-HATI)')
    if st.checkbox('Tunjukkan opsi reset DB'):
        if st.button('Reset seluruh DB (hapus items & transactions & users)'):
            with closing(get_conn()) as conn:
                cur = conn.cursor()
                cur.execute('DROP TABLE IF EXISTS transactions')
                cur.execute('DROP TABLE IF EXISTS items')
                cur.execute('DROP TABLE IF EXISTS users')
                conn.commit()
            st.success('DB telah direset. App akan menginisialisasi ulang DB. Silakan refresh.')

# End of file
