# coordination_app.py

"""
Coordination App for Dispatcher Turn Around
------------------------------------------

This application allows a coordinator to upload a daily flight list
from an Excel file (the same format used by the old roster app),
view the flights sorted by scheduled arrival time (STA), edit
variable fields such as SLOTS and FLIGHT PLAN, and publish the
resulting data to Firebase so that the dispatcher turn-around app
can display up-to-date information.

The app uses Tkinter for the GUI and pandas for data parsing.  The
Firebase integration is optional: if the firebase_admin package is
installed and configured with your service account and database
URL, pressing the "Publish to Firebase" button will upload all
flights under the `/flights` node.

Instructions:

1. Install dependencies (pandas, openpyxl, firebase_admin) using pip if
   they are not already installed.
2. Place your Firebase service account JSON in the same directory as
   this script and update the `FIREBASE_CRED_FILE` and
   `FIREBASE_DB_URL` constants below.
3. Run the script: `python coordination_app.py`
4. Click "Load Excel" to select your pair report (.xlsx) file.
5. Modify the "Slot" and "Flight Plan" columns as needed.
6. Click "Publish to Firebase" to upload the current table to
   Firebase.

This tool is intended to be a starting point. You can extend it
by adding validation, filtering by date, or integrating with your
existing roster logic.
"""

import os
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict

import pandas as pd
from datetime import datetime, timedelta

try:
    import firebase_admin
    from firebase_admin import credentials, db
except ImportError:
    # Firebase is optional; if unavailable, publishing will be disabled.
    firebase_admin = None

# ----- Configuration -----
FIREBASE_CRED_FILE = 'serviceAccountKey.json'
FIREBASE_DB_URL = 'https://turn-around-fa74b-default-rtdb.europe-west1.firebasedatabase.app'


@dataclass
class FlightRecord:
    flight: str
    sta: Optional[pd.Timestamp]
    std: Optional[pd.Timestamp]
    registration: str
    aircraft_type: str
    airline: str
    slot: str = ""
    flight_plan: str = ""
    eta: str = ""
    parking: str = ""
    airline_code: str = ""

    def to_firebase_dict(self) -> Dict[str, str]:
        sta_str = self.sta.isoformat() if pd.notna(self.sta) else ""
        std_str = self.std.isoformat() if pd.notna(self.std) else ""
        return {
            "flightNumber": self.flight,
            "sta": sta_str,
            "std": std_str,
            "registration": self.registration,
            "aircraftType": self.aircraft_type,
            "airline": self.airline,
            "slot": self.slot,
            "flightPlan": self.flight_plan,
            "eta": self.eta,
            "parking": self.parking,
            "airlineCode": self.airline_code,
        }


class CoordinationApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Coordination App")
        self.root.geometry("1000x600")

        try:
            self.root.configure(bg="#eef5ff")
        except Exception:
            pass

        self.all_flight_records: List[FlightRecord] = []
        self.flight_records: List[FlightRecord] = []
        self.airline_settings: Dict[str, Dict[str, str]] = {}

        # default filter: today's flights
        self.filter_type = 'Day'
        self.filter_date = datetime.now().date()

        self.create_widgets()
        self.setup_firebase()
        self.load_airline_settings()
        self.load_existing_flights()

    # ---------------- UI ----------------
    def create_widgets(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass

        style.configure(
            "Modern.Treeview",
            background="#ffffff",
            foreground="#333333",
            fieldbackground="#ffffff",
            rowheight=24,
            font=("Arial", 10),
        )
        style.configure(
            "Modern.Treeview.Heading",
            background="#003366",
            foreground="#ffffff",
            font=("Arial", 10, "bold"),
        )
        style.map("Modern.Treeview", background=[('selected', '#b3d7ff')])

        controls_frame = tk.Frame(self.root, bg="#00366f")
        controls_frame.pack(fill=tk.X, padx=10, pady=5)

        def make_button(parent, text, command, bg="#007bff", fg="#ffffff"):
            return tk.Button(
                parent,
                text=text,
                command=command,
                bg=bg,
                fg=fg,
                activebackground="#0059b3",
                activeforeground="#ffffff",
                relief=tk.FLAT,
                font=("Arial", 10, "bold"),
                padx=10,
                pady=5,
            )

        load_btn = make_button(controls_frame, "Load Excel", self.load_excel)
        load_btn.pack(side=tk.LEFT, padx=(0, 5))

        publish_btn = make_button(controls_frame, "Publish", self.publish_to_firebase, bg="#28a745")
        publish_btn.pack(side=tk.LEFT, padx=5)

        stats_btn = make_button(controls_frame, "Flight Stats", self.show_stats, bg="#17a2b8")
        stats_btn.pack(side=tk.LEFT, padx=5)

        settings_btn = make_button(controls_frame, "Settings", self.open_settings, bg="#ffc107", fg="#333333")
        settings_btn.pack(side=tk.LEFT, padx=5)

        filter_frame = tk.Frame(controls_frame, bg="#00366f")
        filter_frame.pack(side=tk.LEFT, padx=(20, 0))
        tk.Label(filter_frame, text="Display:", bg="#00366f", fg="#ffffff",
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))

        self.filter_type_var = tk.StringVar(value=self.filter_type)
        self.filter_type_cb = ttk.Combobox(
            filter_frame,
            values=["Day", "Week", "Month", "Year", "All"],
            state='readonly',
            width=6,
            textvariable=self.filter_type_var
        )
        self.filter_type_cb.pack(side=tk.LEFT, padx=(0, 5))

        tk.Label(filter_frame, text="Date:", bg="#00366f", fg="#ffffff",
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))

        self.filter_date_var = tk.StringVar(value=self.filter_date.strftime("%Y-%m-%d"))
        self.filter_date_entry = tk.Entry(filter_frame, textvariable=self.filter_date_var, width=10)
        self.filter_date_entry.pack(side=tk.LEFT, padx=(0, 5))

        apply_btn = tk.Button(
            filter_frame, text="Apply", command=self.apply_filter,
            bg="#20c997", fg="#ffffff", relief=tk.FLAT,
            font=("Arial", 9, "bold"), padx=8, pady=2
        )
        apply_btn.pack(side=tk.LEFT)

        tree_frame = tk.Frame(self.root, bg="#eef5ff")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = (
            "flight", "sta", "std", "eta", "registration", "aircraft_type",
            "airline", "slot", "flight_plan", "parking"
        )

        self.tree = ttk.Treeview(tree_frame, columns=columns,
                                 show="headings", style="Modern.Treeview")

        self.tree.heading("flight", text="Flight")
        self.tree.heading("sta", text="STA")
        self.tree.heading("std", text="STD")
        self.tree.heading("eta", text="ETA")
        self.tree.heading("registration", text="Registration")
        self.tree.heading("aircraft_type", text="Aircraft Type")
        self.tree.heading("airline", text="Airline")
        self.tree.heading("slot", text="Slot")
        self.tree.heading("flight_plan", text="Flight Plan")
        self.tree.heading("parking", text="Parking")

        self.tree.column("flight", width=80)
        self.tree.column("sta", width=120)
        self.tree.column("std", width=120)
        self.tree.column("eta", width=120)
        self.tree.column("registration", width=100)
        self.tree.column("aircraft_type", width=120)
        self.tree.column("airline", width=120)
        self.tree.column("slot", width=100)
        self.tree.column("flight_plan", width=100)
        self.tree.column("parking", width=80)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self.on_double_click)

    # ------------- Firebase init / preload -------------
    def setup_firebase(self) -> None:
        if firebase_admin is None or not FIREBASE_CRED_FILE or not FIREBASE_DB_URL:
            return
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CRED_FILE)
                firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
        except Exception as e:
            messagebox.showerror("Firebase Error", f"Failed to initialize Firebase: {e}")

    def load_existing_flights(self) -> None:
        if firebase_admin is None or not FIREBASE_CRED_FILE or not FIREBASE_DB_URL:
            return
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CRED_FILE)
                firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            flights_ref = db.reference('/flights')
            data = flights_ref.get() or {}
            records: List[FlightRecord] = []
            for flight_id, info in data.items():
                if not isinstance(info, dict):
                    continue
                flight_num = info.get('flightNumber', '') or info.get('flight', '') or ''
                sta_iso = info.get('sta', '') or ''
                std_iso = info.get('std', '') or ''
                sta = pd.to_datetime(sta_iso) if sta_iso else pd.NaT
                std = pd.to_datetime(std_iso) if std_iso else pd.NaT
                registration = info.get('registration', '') or ''
                aircraft_type = info.get('aircraftType', '') or ''
                airline_name = info.get('airline', '') or ''
                slot = info.get('slot', '') or ''
                flight_plan = info.get('flightPlan', '') or ''
                eta = info.get('eta', '') or ''
                parking = info.get('parking', '') or ''
                airline_code = info.get('airlineCode', '') or self.get_airline_code(flight_num)
                if not airline_name and self.airline_settings.get(airline_code):
                    airline_name = self.airline_settings[airline_code].get('name', '')

                rec = FlightRecord(
                    flight=flight_num,
                    sta=sta,
                    std=std,
                    registration=registration,
                    aircraft_type=aircraft_type,
                    airline=airline_name,
                    slot=slot,
                    flight_plan=flight_plan,
                    eta=eta,
                    parking=parking,
                    airline_code=airline_code
                )
                records.append(rec)

            if records:
                records.sort(key=lambda x: (pd.NaT if pd.isna(x.sta) else x.sta))
                self.all_flight_records = records
                self.flight_records = self.filter_records(self.all_flight_records)
                self.refresh_treeview()
        except Exception as ex:
            print(f"Error loading existing flights: {ex}")

    # ------------- Excel loading / parsing -------------
    def load_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Select flight list Excel file",
            filetypes=[("Excel files", "*.xlsx;*.xls")]
        )
        if not path:
            return
        try:
            records = self.parse_excel(path)
            records.sort(key=lambda x: (pd.NaT if pd.isna(x.sta) else x.sta))
            self.all_flight_records = records
            self.flight_records = self.filter_records(self.all_flight_records)
        except Exception as e:
            messagebox.showerror("Error loading file", f"Failed to load Excel file: {e}")
            return
        self.refresh_treeview()

    def parse_excel(self, path: str) -> List[FlightRecord]:
        try:
            xls = pd.ExcelFile(path)
            sheet_name = 'pair_report' if 'pair_report' in xls.sheet_names else xls.sheet_names[0]
        except Exception:
            sheet_name = 0

        raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
        header_row_idx = None
        for i in range(min(30, len(raw))):
            row_vals = [str(x).strip().upper() if pd.notna(x) else "" for x in raw.iloc[i].tolist()]
            has_flight = any("FLIGHT" in val for val in row_vals)
            has_sta = any(any(cand in val for cand in ["STA", "ARR", "ARRIVAL"]) for val in row_vals)
            has_std = any(any(cand in val for cand in ["STD", "DEP", "DEPARTURE", "ETD"]) for val in row_vals)
            if has_flight and (has_sta or has_std):
                header_row_idx = i
                break

        if header_row_idx is not None:
            df = pd.read_excel(path, sheet_name=sheet_name, header=header_row_idx)
        else:
            df = pd.read_excel(path, sheet_name=sheet_name, header=0)

        columns_upper = [str(col).strip().upper() for col in df.columns]

        def find_column(candidates: List[str]) -> Optional[str]:
            for col, upper in zip(df.columns, columns_upper):
                for cand in candidates:
                    if cand == upper or cand in upper:
                        return col
            return None

        flight_col = None
        for col in df.columns:
            if str(col).strip().upper() == "FLIGHT.1":
                flight_col = col
                break
        if flight_col is None:
            flight_candidates = [col for col, upper in zip(df.columns, columns_upper) if "FLIGHT" in upper]
            if flight_candidates:
                flight_col = flight_candidates[-1]

        sta_col = find_column(["STA", "ARR", "ARRIVAL"])
        eta_col = find_column(["ETA", "EST", "EXPECTED ARRIVAL"])
        dep_col = find_column(["STD", "DEP", "DEPARTURE", "ETD"])
        parking_col = find_column(["PARKING", "PARK", "STAND", "STATION", "POSITION"])
        reg_col = find_column(["REG", "REGISTRATION", "TREG", "TAIL", "REG.NO"])
        type_col = find_column(["TYPE", "AIRCRAFT", "A/C", "ACFT", "PLANE", "A/T"])
        airline_col = find_column(["AIRLINE", "COMPANY", "CARRIER", "OPERATOR"])

        if flight_col is None:
            raise ValueError("Could not find a flight number column in the Excel file.")

        sta_series = pd.to_datetime(df[sta_col], errors="coerce", dayfirst=True) if sta_col else pd.Series([pd.NaT] * len(df))
        std_series = pd.to_datetime(df[dep_col], errors="coerce", dayfirst=True) if dep_col else pd.Series([pd.NaT] * len(df))

        records: List[FlightRecord] = []
        for i, row in df.iterrows():
            flight = str(row[flight_col]).strip() if pd.notna(row[flight_col]) else ""
            if not flight:
                continue
            sta = sta_series.iloc[i]
            std = std_series.iloc[i]
            registration = str(row[reg_col]).strip() if reg_col and pd.notna(row.get(reg_col)) else ""
            aircraft_type = str(row[type_col]).strip() if type_col and pd.notna(row.get(type_col)) else ""
            airline = str(row[airline_col]).strip() if airline_col and pd.notna(row.get(airline_col)) else ""

            eta = ""
            if eta_col and pd.notna(row.get(eta_col)):
                eta_val = row.get(eta_col)
                if isinstance(eta_val, (pd.Timestamp,)):
                    eta = eta_val.strftime("%Y-%m-%d %H:%M")
                else:
                    eta = str(eta_val).strip()

            parking = ""
            if parking_col and pd.notna(row.get(parking_col)):
                parking = str(row.get(parking_col)).strip()

            airline_code = self.get_airline_code(flight)
            mapped_name = None
            if hasattr(self, 'airline_settings') and self.airline_settings:
                mapping = self.airline_settings.get(airline_code)
                if mapping:
                    mapped_name = mapping.get('name', None)

            if airline:
                airline_name = airline
            elif mapped_name:
                airline_name = mapped_name
            else:
                airline_name = ""

            rec = FlightRecord(
                flight=flight,
                sta=sta,
                std=std,
                registration=registration,
                aircraft_type=aircraft_type,
                airline=airline_name,
                eta=eta,
                parking=parking,
                airline_code=airline_code,
            )
            records.append(rec)

        records.sort(key=lambda x: (pd.NaT if pd.isna(x.sta) else x.sta))
        return records

    # ------------- filtering / refresh -------------
    def refresh_treeview(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for idx, rec in enumerate(self.flight_records):
            sta_str = rec.sta.strftime("%Y-%m-%d %H:%M") if pd.notna(rec.sta) else ""
            std_str = rec.std.strftime("%Y-%m-%d %H:%M") if pd.notna(rec.std) else ""
            eta_str = rec.eta or ""
            values = (
                rec.flight,
                sta_str,
                std_str,
                eta_str,
                rec.registration,
                rec.aircraft_type,
                rec.airline,
                rec.slot,
                rec.flight_plan,
                rec.parking,
            )
            self.tree.insert("", "end", iid=str(idx), values=values)

    def filter_records(self, records: List[FlightRecord]) -> List[FlightRecord]:
        ftype = getattr(self, 'filter_type', 'All')
        fdate = getattr(self, 'filter_date', None)
        if ftype == 'All' or fdate is None:
            return list(records)
        filtered: List[FlightRecord] = []
        for rec in records:
            dt = None
            try:
                if rec.sta is not None and pd.notna(rec.sta):
                    dt = rec.sta.date()
                elif rec.std is not None and pd.notna(rec.std):
                    dt = rec.std.date()
            except Exception:
                dt = None
            if dt is None:
                continue
            if ftype == 'Day':
                if dt == fdate:
                    filtered.append(rec)
            elif ftype == 'Week':
                if dt.isocalendar()[:2] == fdate.isocalendar()[:2]:
                    filtered.append(rec)
            elif ftype == 'Month':
                if dt.year == fdate.year and dt.month == fdate.month:
                    filtered.append(rec)
            elif ftype == 'Year':
                if dt.year == fdate.year:
                    filtered.append(rec)
        return filtered

    def apply_filter(self) -> None:
        if hasattr(self, 'filter_type_var'):
            self.filter_type = self.filter_type_var.get()
        if hasattr(self, 'filter_date_var'):
            date_str = self.filter_date_var.get().strip()
            try:
                self.filter_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                messagebox.showwarning("Date Format", "Please enter date as YYYY-MM-DD.")
                return
        self.flight_records = self.filter_records(self.all_flight_records)
        self.refresh_treeview()

    # ------------- editing -------------
    def on_double_click(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_index = int(col_id.replace("#", "")) - 1
        if col_index <= 2:
            return
        current_value = self.tree.item(row_id, "values")[col_index]

        x, y, width, height = self.tree.bbox(row_id, col_id)
        entry = tk.Entry(self.tree)
        entry.place(x=x, y=y, width=width, height=height)
        entry.insert(0, current_value)
        entry.focus_set()

        def on_enter(event):
            new_value = entry.get()
            idx = int(row_id)
            rec = self.flight_records[idx]
            if col_index == 3:
                rec.eta = new_value
            elif col_index == 4:
                rec.registration = new_value
            elif col_index == 5:
                rec.aircraft_type = new_value
            elif col_index == 6:
                rec.airline = new_value
            elif col_index == 7:
                rec.slot = new_value
            elif col_index == 8:
                rec.flight_plan = new_value
            elif col_index == 9:
                rec.parking = new_value

            values = list(self.tree.item(row_id, "values"))
            values[col_index] = new_value
            self.tree.item(row_id, values=values)
            entry.destroy()

        def on_escape(event):
            entry.destroy()

        entry.bind("<Return>", on_enter)
        entry.bind("<FocusOut>", on_escape)

    # ------------- Firebase publish -------------
    def publish_to_firebase(self) -> None:
        if firebase_admin is None or not FIREBASE_CRED_FILE or not FIREBASE_DB_URL:
            messagebox.showwarning(
                "Firebase not configured",
                "Firebase credentials or URL missing. Please configure FIREBASE_CRED_FILE and FIREBASE_DB_URL in the script.",
            )
            return
        if not firebase_admin._apps:
            try:
                cred = credentials.Certificate(FIREBASE_CRED_FILE)
                firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            except Exception as e:
                messagebox.showerror("Firebase Error", f"Failed to initialize Firebase: {e}")
                return
        try:
            flights_ref = db.reference("/flights")
            count = 0
            for rec in self.flight_records:
                flight_id = f"{rec.flight.replace(' ', '')}_{rec.sta.strftime('%Y%m%d') if pd.notna(rec.sta) else ''}"
                data = rec.to_firebase_dict()
                update_dict = {}
                for k, v in data.items():
                    if k == "eta":
                        update_dict[k] = v
                    else:
                        if v not in (None, ""):
                            update_dict[k] = v
                flights_ref.child(flight_id).update(update_dict)
                count += 1
            messagebox.showinfo("Success", f"Uploaded {count} flights to Firebase.")
        except Exception as e:
            messagebox.showerror("Upload Error", f"Failed to upload to Firebase: {e}")

    # ------------- airline settings -------------
    def get_airline_code(self, flight_number: str) -> str:
        if not flight_number:
            return ""
        prefix = ""
        for ch in flight_number:
            if ch.isalpha():
                prefix += ch
            else:
                break
        if len(prefix) >= 3:
            return prefix[:3].upper()
        elif len(prefix) >= 2:
            return prefix[:2].upper()
        else:
            return prefix.upper()

    def load_airline_settings(self) -> None:
        settings_path = os.path.join(os.path.dirname(__file__), 'airline_settings.json')
        if os.path.exists(settings_path):
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    self.airline_settings = json.load(f)
            except Exception:
                self.airline_settings = {}
        else:
            self.airline_settings = {}
        for code, data in list(self.airline_settings.items()):
            if not isinstance(data, dict):
                self.airline_settings[code] = {
                    'name': '',
                    'instructions': data,
                    'layoutUrl': '',
                    'types': {}
                }
            else:
                if 'types' not in data:
                    data['types'] = {}

    def save_airline_settings(self) -> None:
        settings_path = os.path.join(os.path.dirname(__file__), 'airline_settings.json')
        try:
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(self.airline_settings, f, indent=2)
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to write airline settings: {e}")
            return

        if firebase_admin is not None and self.airline_settings:
            try:
                if not firebase_admin._apps:
                    cred = credentials.Certificate(FIREBASE_CRED_FILE)
                    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
                instructions_data = {}
                for code, data in self.airline_settings.items():
                    entry = {
                        'name': data.get('name', ''),
                        'instructions': data.get('instructions', ''),
                        'layoutUrl': data.get('layoutUrl', ''),
                        'types': {}
                    }
                    for typ, tdata in data.get('types', {}).items():
                        entry['types'][typ] = {
                            'instructions': tdata.get('instructions', ''),
                            'layoutUrl': tdata.get('layoutUrl', '')
                        }
                    instructions_data[code] = entry
                db.reference('/airlineInstructions').set(instructions_data)
            except Exception as e:
                messagebox.showwarning("Firebase Warning", f"Failed to upload airline instructions: {e}")

    def open_settings(self) -> None:
        SettingsWindow(self, self.airline_settings)

    # ------------- stats window -------------
    def show_stats(self) -> None:
        if firebase_admin is None or not FIREBASE_CRED_FILE or not FIREBASE_DB_URL:
            messagebox.showwarning(
                "Firebase not configured",
                "Firebase credentials or URL missing. Please configure FIREBASE_CRED_FILE and FIREBASE_DB_URL in the script.",
            )
            return
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CRED_FILE)
                firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
        except Exception as e:
            messagebox.showerror("Firebase Error", f"Failed to initialize Firebase: {e}")
            return
        try:
            ops_ref = db.reference("/flightOperations")
            ops_data = ops_ref.get() or {}
            StatsWindow(self.root, ops_data, self.airline_settings)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to fetch stats: {e}")



# ----------------------- Advanced Statistics Window -----------------------
class StatsWindow(tk.Toplevel):
    """
    Presents an interactive statistics dashboard organised into multiple
    sections: Services, Checklists, Turnaround and Airlines.  It supports
    filtering by timeframe (all data, last 7/30/365 days) and provides
    service‑specific metrics upon request via dedicated buttons.

    Parameters
    ----------
    master : tk.Widget
        Parent widget (usually the root of the coordination app).
    ops_data : dict
        Raw flight operations data from Firebase's `/flightOperations` node.
    airline_settings : dict
        Mapping of airline codes to settings including the airline name and
        type‑specific instructions.  Used to display human readable airline
        names in tables.
    """

    def __init__(self, master: tk.Widget, ops_data: dict, airline_settings: Dict[str, Dict[str, str]]) -> None:
        super().__init__(master)
        self.title("Flight Statistics")
        self.geometry("1250x750")
        try:
            self.configure(bg="#eef5ff")
        except Exception:
            pass
        # Store provided data
        self.ops_data = ops_data or {}
        self.airline_settings = airline_settings or {}
        # Initialize period filter: default to today's day.
        self.period_type_var = tk.StringVar(value="Day")
        self.period_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        # Service options
        self.service_options = ["GPU", "ACU", "Toilet", "Water"]
        self.selected_service = tk.StringVar(value=self.service_options[0])
        # Preprocess flight records for quick filtering
        self.flight_records_all = []  # All flights regardless of timeframe
        self.preprocess_flights()
        # Create UI
        self.create_widgets()
        # Compute initial metrics and populate views
        self.update_data()

    # ---------------------------------------------------------------------
    # Data processing
    # ---------------------------------------------------------------------
    def preprocess_flights(self) -> None:
        """Convert raw ops_data into a list of per‑flight dictionaries with all necessary fields."""
        records = []
        for flight_id, ops in self.ops_data.items():
            if not isinstance(ops, dict):
                continue
            # Determine flight number and date from the key (e.g. "TU1234_20240101")
            flight_parts = str(flight_id).split('_')
            flight_number = flight_parts[0] if flight_parts else str(flight_id)
            flight_date = None
            if len(flight_parts) > 1:
                date_str = flight_parts[1]
                try:
                    # Parse flight date from the key.  Always convert to a
                    # plain date rather than a datetime to allow equality
                    # comparisons with a date object when filtering by day.
                    dt_tmp = datetime.strptime(date_str, "%Y%m%d")
                    flight_date = dt_tmp.date()
                except Exception:
                    flight_date = None
            # Use checkTimes or operations times as fallback for date
            if flight_date is None:
                # Try to find a date from operations or checkTimes entries
                times = []
                for d in ops.get('operations', {}).values():
                    if isinstance(d, dict):
                        if d.get('startTime'):
                            times.append(d['startTime'])
                        if d.get('finishTime'):
                            times.append(d['finishTime'])
                for d in ops.get('checkTimes', {}).values():
                    times.append(d)
                if times:
                    try:
                        dt0 = pd.to_datetime(times[0])
                        # Always convert to plain date
                        flight_date = dt0.date()
                    except Exception:
                        flight_date = None
            # Determine airline code and name
            airline_code = self.get_airline_code(flight_number)
            airline_name = self.airline_settings.get(airline_code, {}).get('name', airline_code)
            # Parse checklists
            checklist = ops.get('checklist', {}) or {}
            chocks = bool(checklist.get('chocks'))
            cones = bool(checklist.get('cones'))
            fod = bool(checklist.get('fod'))
            security = bool(checklist.get('security'))
            toilet = bool(checklist.get('toilet'))
            water = bool(checklist.get('water'))
            # Parse checkTimes for doors
            check_times = ops.get('checkTimes', {}) or {}
            door_open_iso = check_times.get('doorsOpen', '') or ''
            door_close_iso = check_times.get('doorsClosed', '') or ''
            door_open_str = self.format_time(door_open_iso)
            door_close_str = self.format_time(door_close_iso)
            turnaround_duration = None
            if door_open_iso and door_close_iso:
                try:
                    t_open = pd.to_datetime(door_open_iso)
                    t_close = pd.to_datetime(door_close_iso)
                    turnaround_duration = (t_close - t_open).total_seconds() / 60
                except Exception:
                    turnaround_duration = None
            # Parse operations durations
            operations = ops.get('operations', {}) or {}
            def get_op_duration(key: str):
                times = operations.get(key, {}) if isinstance(operations.get(key, {}), dict) else {}
                start_iso = times.get('startTime', '') or ''
                finish_iso = times.get('finishTime', '') or ''
                start_str = self.format_time(start_iso)
                finish_str = self.format_time(finish_iso)
                dur = None
                if start_iso and finish_iso:
                    try:
                        s_dt = pd.to_datetime(start_iso)
                        f_dt = pd.to_datetime(finish_iso)
                        dur = (f_dt - s_dt).total_seconds() / 60
                    except Exception:
                        dur = None
                return start_str, finish_str, dur
            # GPU
            gpu_start, gpu_finish, gpu_duration = get_op_duration('gpu')
            # ACU/APU
            if 'apu' in operations:
                apu_start, apu_finish, apu_duration = get_op_duration('apu')
            else:
                apu_start, apu_finish, apu_duration = get_op_duration('acu')
            # Turnaround sub operations durations
            unload_dur = get_op_duration('unloading')[2]
            disembark_dur = get_op_duration('disembarking')[2]
            clean_dur = get_op_duration('cleaning')[2]
            load_dur = get_op_duration('loading')[2]
            board_dur = get_op_duration('boarding')[2]
            # Remarks
            remarks = ops.get('remarks', '') or ''
            # Build record
            rec = {
                'flight': flight_id,
                'flightNumber': flight_number,
                'date': flight_date,
                'airline_code': airline_code,
                'airline_name': airline_name,
                'gpuStart': gpu_start, 'gpuFinish': gpu_finish, 'gpuDuration': gpu_duration,
                'apuStart': apu_start, 'apuFinish': apu_finish, 'apuDuration': apu_duration,
                'toilet': toilet, 'water': water,
                'chocks': chocks, 'cones': cones, 'fod': fod, 'security': security,
                'doorOpen': door_open_str, 'doorClose': door_close_str, 'turnaroundDuration': turnaround_duration,
                'unloadingDuration': unload_dur, 'disembarkingDuration': disembark_dur,
                'cleaningDuration': clean_dur, 'loadingDuration': load_dur, 'boardingDuration': board_dur,
                'remarks': remarks,
            }
            records.append(rec)
        self.flight_records_all = records

    # Utility functions used in preprocessing
    @staticmethod
    def get_airline_code(flight_number: str) -> str:
        if not flight_number:
            return ''
        prefix = ''
        for ch in flight_number:
            if ch.isalpha():
                prefix += ch
            else:
                break
        if len(prefix) >= 3:
            return prefix[:3].upper()
        elif len(prefix) >= 2:
            return prefix[:2].upper()
        return prefix.upper()

    @staticmethod
    def format_time(iso_string: str) -> str:
        if not iso_string:
            return ''
        try:
            dt = pd.to_datetime(iso_string)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return iso_string

    # ---------------------------------------------------------------------
    # UI creation
    # ---------------------------------------------------------------------
    def create_widgets(self) -> None:
        """Initialise the statistics window UI components."""
        # Top frame for timeframe selection
        # Top frame for period selection (Day, Week, Month, Year, All) and date entry
        top_frame = tk.Frame(self, bg="#eef5ff")
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        tk.Label(top_frame, text="Period:", bg="#eef5ff", font=("Arial", 9, "bold"), fg="#003366").pack(side=tk.LEFT)
        # Combobox for period type
        type_cb = ttk.Combobox(top_frame, values=["Day", "Week", "Month", "Year", "All"], state='readonly', textvariable=self.period_type_var, width=7)
        type_cb.pack(side=tk.LEFT, padx=(5, 10))
        # Date entry field
        tk.Label(top_frame, text="Date:", bg="#eef5ff", font=("Arial", 9, "bold"), fg="#003366").pack(side=tk.LEFT)
        date_entry = tk.Entry(top_frame, textvariable=self.period_date_var, width=10)
        date_entry.pack(side=tk.LEFT, padx=(5, 10))
        # Apply button to trigger data refresh
        apply_btn = tk.Button(top_frame, text="Apply", command=self.update_data, bg="#20c997", fg="#ffffff", relief=tk.FLAT, font=("Arial", 9, "bold"), padx=8, pady=2)
        apply_btn.pack(side=tk.LEFT)
        # Notebook for sections
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        # Tabs
        self.services_tab = tk.Frame(self.notebook, bg="#eef5ff")
        self.checklist_tab = tk.Frame(self.notebook, bg="#eef5ff")
        self.turnaround_tab = tk.Frame(self.notebook, bg="#eef5ff")
        self.airline_tab = tk.Frame(self.notebook, bg="#eef5ff")
        self.reports_tab = tk.Frame(self.notebook, bg="#eef5ff")
        self.notebook.add(self.services_tab, text="Services")
        self.notebook.add(self.checklist_tab, text="Checklists")
        self.notebook.add(self.turnaround_tab, text="Turnaround")
        self.notebook.add(self.airline_tab, text="Airlines")
        self.notebook.add(self.reports_tab, text="Reports")
        # Build service tab layout: filter row and containers
        self.build_services_tab()
        # Build other tabs containers (will populate later)
        self.checklist_content = tk.Frame(self.checklist_tab, bg="#eef5ff")
        self.checklist_content.pack(fill=tk.BOTH, expand=True)
        self.turnaround_content = tk.Frame(self.turnaround_tab, bg="#eef5ff")
        self.turnaround_content.pack(fill=tk.BOTH, expand=True)
        self.airline_content = tk.Frame(self.airline_tab, bg="#eef5ff")
        self.airline_content.pack(fill=tk.BOTH, expand=True)
        self.reports_content = tk.Frame(self.reports_tab, bg="#eef5ff")
        self.reports_content.pack(fill=tk.BOTH, expand=True)

    def build_services_tab(self) -> None:
        """Create the filter buttons and container frames for the services tab."""
        # Filter buttons
        filter_frame = tk.Frame(self.services_tab, bg="#eef5ff")
        filter_frame.pack(fill=tk.X, pady=(5, 5))
        for svc in self.service_options:
            btn = tk.Button(filter_frame, text=svc, relief=tk.FLAT, font=("Arial", 9, "bold"),
                            command=lambda s=svc: self.update_service(s))
            # Use different colours for active/inactive states
            btn.configure(bg="#007bff", fg="#ffffff", activebackground="#005bb5", activeforeground="#ffffff")
            btn.pack(side=tk.LEFT, padx=5)
            # Save reference for styling later
        self.service_buttons = filter_frame.winfo_children()
        # KPI and table containers
        self.services_kpi_frame = tk.Frame(self.services_tab, bg="#eef5ff")
        self.services_kpi_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.services_table_container = tk.Frame(self.services_tab, bg="#eef5ff")
        self.services_table_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))
        self.services_summary_container = tk.Frame(self.services_tab, bg="#eef5ff")
        self.services_summary_container.pack(fill=tk.BOTH, expand=False, padx=5, pady=(0, 5))

    # ---------------------------------------------------------------------
    # Event handlers
    # ---------------------------------------------------------------------
    def update_timeframe(self, event=None) -> None:
        """Handle changes in the timeframe combobox."""
        # Recompute and refresh views for the selected timeframe
        self.update_data()

    def update_service(self, service: str) -> None:
        """Handle clicking of a service filter button."""
        if service not in self.service_options:
            return
        self.selected_service.set(service)
        # Refresh only the services tab metrics and tables
        self.update_service_view()

    # ---------------------------------------------------------------------
    # Data computation
    # ---------------------------------------------------------------------
    def compute_data(self) -> None:
        """
        Compute aggregated metrics according to the selected period and
        reference date.  The period type may be Day, Week, Month, Year or
        All.  For Day, only flights on the same date are included; for
        Week, flights in the same ISO week; for Month, flights in the
        same year and month; for Year, flights in the same calendar
        year.  If period type is All or the date cannot be parsed,
        include all flights.
        """
        # Determine the period type and reference date
        period_type = self.period_type_var.get() if hasattr(self, 'period_type_var') else 'All'
        date_str = self.period_date_var.get() if hasattr(self, 'period_date_var') else ''
        ref_date = None
        if period_type != 'All':
            try:
                ref_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                ref_date = None
        # Filter flights accordingly
        if period_type == 'All' or ref_date is None:
            flights = self.flight_records_all[:]
        else:
            flights = []
            for rec in self.flight_records_all:
                d = rec.get('date')
                if d is None:
                    continue
                if period_type == 'Day':
                    if d == ref_date:
                        flights.append(rec)
                elif period_type == 'Week':
                    if d.isocalendar()[:2] == ref_date.isocalendar()[:2]:
                        flights.append(rec)
                elif period_type == 'Month':
                    if d.year == ref_date.year and d.month == ref_date.month:
                        flights.append(rec)
                elif period_type == 'Year':
                    if d.year == ref_date.year:
                        flights.append(rec)
        # Store filtered flights for use in UI updates
        self.filtered_records = flights
        # Initialise aggregates
        self.services_totals = {
            'GPU': {'count': 0, 'time': 0.0, 'min': float('inf'), 'max': 0.0, 'airline_counts': {}, 'airline_times': {}},
            'ACU': {'count': 0, 'time': 0.0, 'min': float('inf'), 'max': 0.0, 'airline_counts': {}, 'airline_times': {}},
            'Toilet': {'count': 0, 'airline_counts': {}},
            'Water': {'count': 0, 'airline_counts': {}},
        }
        self.services_flight_rows = {'GPU': [], 'ACU': [], 'Toilet': [], 'Water': []}
        # Checklist totals
        chk_counts = {'chocks': 0, 'cones': 0, 'fod': 0, 'security': 0, 'toilet': 0, 'water': 0}
        chk_total = 0
        # Checklist counts per airline.  Each entry tracks how many flights
        # and how many completions per item for that airline.
        # Example structure: { 'TU': { 'flights': 3, 'chocks': 3, 'cones': 2, ... } }
        airline_checklists = {}
        # Turnaround totals
        tr = {
            'count': 0,
            'time': 0.0,
            'min': float('inf'),
            'max': 0.0,
            'exceed45': 0,
            'op_times': {
                'unloading': {'time': 0.0, 'count': 0},
                'disembarking': {'time': 0.0, 'count': 0},
                'cleaning': {'time': 0.0, 'count': 0},
                'loading': {'time': 0.0, 'count': 0},
                'boarding': {'time': 0.0, 'count': 0},
            }
        }
        # Airline metrics
        airline_metrics = {}
        for rec in flights:
            code = rec['airline_code'] or 'N/A'
            name = rec['airline_name']
            # GPU
            if rec['gpuDuration'] is not None:
                dur = rec['gpuDuration']
                self.services_totals['GPU']['count'] += 1
                self.services_totals['GPU']['time'] += dur
                self.services_totals['GPU']['min'] = min(self.services_totals['GPU']['min'], dur)
                self.services_totals['GPU']['max'] = max(self.services_totals['GPU']['max'], dur)
                # per airline
                self.services_totals['GPU']['airline_counts'][code] = self.services_totals['GPU']['airline_counts'].get(code, 0) + 1
                self.services_totals['GPU']['airline_times'][code] = self.services_totals['GPU']['airline_times'].get(code, 0.0) + dur
                # record row
                self.services_flight_rows['GPU'].append([
                    rec['flight'], name, rec['gpuStart'], rec['gpuFinish'], f"{dur:.1f}"
                ])
            # ACU/APU
            if rec['apuDuration'] is not None:
                dur = rec['apuDuration']
                self.services_totals['ACU']['count'] += 1
                self.services_totals['ACU']['time'] += dur
                self.services_totals['ACU']['min'] = min(self.services_totals['ACU']['min'], dur)
                self.services_totals['ACU']['max'] = max(self.services_totals['ACU']['max'], dur)
                self.services_totals['ACU']['airline_counts'][code] = self.services_totals['ACU']['airline_counts'].get(code, 0) + 1
                self.services_totals['ACU']['airline_times'][code] = self.services_totals['ACU']['airline_times'].get(code, 0.0) + dur
                self.services_flight_rows['ACU'].append([
                    rec['flight'], name, rec['apuStart'], rec['apuFinish'], f"{dur:.1f}"
                ])
            # Toilet
            if rec['toilet']:
                self.services_totals['Toilet']['count'] += 1
                self.services_totals['Toilet']['airline_counts'][code] = self.services_totals['Toilet']['airline_counts'].get(code, 0) + 1
                self.services_flight_rows['Toilet'].append([
                    rec['flight'], name, 'Yes'
                ])
            # Water
            if rec['water']:
                self.services_totals['Water']['count'] += 1
                self.services_totals['Water']['airline_counts'][code] = self.services_totals['Water']['airline_counts'].get(code, 0) + 1
                self.services_flight_rows['Water'].append([
                    rec['flight'], name, 'Yes'
                ])
            # Checklist
            chk_total += 1
            chk_counts['chocks'] += int(rec['chocks'])
            chk_counts['cones'] += int(rec['cones'])
            chk_counts['fod'] += int(rec['fod'])
            chk_counts['security'] += int(rec['security'])
            chk_counts['toilet'] += int(rec['toilet'])
            chk_counts['water'] += int(rec['water'])
            # Per airline checklist counts
            if code not in airline_checklists:
                airline_checklists[code] = {
                    'flights': 0,
                    'chocks': 0,
                    'cones': 0,
                    'fod': 0,
                    'security': 0,
                    'toilet': 0,
                    'water': 0,
                }
            a_chk = airline_checklists[code]
            a_chk['flights'] += 1
            a_chk['chocks'] += int(rec['chocks'])
            a_chk['cones'] += int(rec['cones'])
            a_chk['fod'] += int(rec['fod'])
            a_chk['security'] += int(rec['security'])
            a_chk['toilet'] += int(rec['toilet'])
            a_chk['water'] += int(rec['water'])
            # Turnaround
            if rec['turnaroundDuration'] is not None:
                dur = rec['turnaroundDuration']
                tr['count'] += 1
                tr['time'] += dur
                tr['min'] = min(tr['min'], dur)
                tr['max'] = max(tr['max'], dur)
                if dur > 45:
                    tr['exceed45'] += 1
            # Ops durations
            for op in ['unloading', 'disembarking', 'cleaning', 'loading', 'boarding']:
                d = rec[f'{op}Duration']
                if d is not None:
                    tr['op_times'][op]['time'] += d
                    tr['op_times'][op]['count'] += 1
            # Airline metrics accumulate
            if code not in airline_metrics:
                airline_metrics[code] = {
                    'name': name,
                    'flights': 0,
                    'turnaround_time': 0.0,
                    'turnaround_count': 0,
                    'gpu_time': 0.0,
                    'gpu_count': 0,
                    'acu_time': 0.0,
                    'acu_count': 0,
                    'cleaning_time': 0.0,
                    'cleaning_count': 0,
                    'disembarking_time': 0.0,
                    'disembarking_count': 0,
                    'unloading_time': 0.0,
                    'unloading_count': 0,
                    'loading_time': 0.0,
                    'loading_count': 0,
                    'boarding_time': 0.0,
                    'boarding_count': 0,
                    'safety_done': 0,
                    'safety_total': 0,
                }
            m = airline_metrics[code]
            m['flights'] += 1
            if rec['turnaroundDuration'] is not None:
                m['turnaround_time'] += rec['turnaroundDuration']
                m['turnaround_count'] += 1
            if rec['gpuDuration'] is not None:
                m['gpu_time'] += rec['gpuDuration']
                m['gpu_count'] += 1
            if rec['apuDuration'] is not None:
                m['acu_time'] += rec['apuDuration']
                m['acu_count'] += 1
            for op in ['cleaning', 'disembarking', 'unloading', 'loading', 'boarding']:
                d = rec[f'{op}Duration']
                if d is not None:
                    m[f'{op}_time'] += d
                    m[f'{op}_count'] += 1
            safety_done = int(rec['chocks']) + int(rec['cones']) + int(rec['fod']) + int(rec['security'])
            m['safety_done'] += safety_done
            m['safety_total'] += 4
        # Compute checklist summary percentages
        self.checklist_summary = {}
        if chk_total > 0:
            for key in chk_counts:
                self.checklist_summary[key] = (chk_counts[key] / chk_total) * 100.0
        else:
            for key in chk_counts:
                self.checklist_summary[key] = 0.0

        # Compute checklist summary by airline: convert counts to percentages
        # Each entry will store flights count and completion percentages per item
        self.checklist_by_airline = {}
        for code, counts in airline_checklists.items():
            flt = counts['flights'] if counts['flights'] else 0
            if flt == 0:
                # Avoid division by zero; still record empty row
                self.checklist_by_airline[code] = {
                    'code': code,
                    'name': self.airline_settings.get(code, {}).get('name', code),
                    'flights': 0,
                    'chocks': 0.0,
                    'cones': 0.0,
                    'fod': 0.0,
                    'security': 0.0,
                    'toilet': 0.0,
                    'water': 0.0,
                }
                continue
            self.checklist_by_airline[code] = {
                'code': code,
                'name': self.airline_settings.get(code, {}).get('name', code),
                'flights': flt,
                'chocks': (counts['chocks'] / flt) * 100.0,
                'cones': (counts['cones'] / flt) * 100.0,
                'fod': (counts['fod'] / flt) * 100.0,
                'security': (counts['security'] / flt) * 100.0,
                'toilet': (counts['toilet'] / flt) * 100.0,
                'water': (counts['water'] / flt) * 100.0,
            }
        # Compute turnaround summary
        self.turnaround_summary = {
            'avg': (tr['time'] / tr['count']) if tr['count'] else 0.0,
            'min': (tr['min'] if tr['min'] != float('inf') else 0.0),
            'max': tr['max'],
            'pct_exceed': (tr['exceed45'] / tr['count']) * 100.0 if tr['count'] else 0.0,
            'ops_avg': {}
        }
        for op, vals in tr['op_times'].items():
            self.turnaround_summary['ops_avg'][op] = (vals['time'] / vals['count']) if vals['count'] else 0.0
        # Compute airline summary
        self.airline_summary = {}
        # Highlights placeholders
        best = None; worst = None; longest_gpu = None; fastest_boarding = None
        for code, m in airline_metrics.items():
            avg_turnaround = (m['turnaround_time'] / m['turnaround_count']) if m['turnaround_count'] else 0.0
            avg_gpu = (m['gpu_time'] / m['gpu_count']) if m['gpu_count'] else 0.0
            avg_acu = (m['acu_time'] / m['acu_count']) if m['acu_count'] else 0.0
            avg_cleaning = (m['cleaning_time'] / m['cleaning_count']) if m['cleaning_count'] else 0.0
            avg_boarding = (m['boarding_time'] / m['boarding_count']) if m['boarding_count'] else 0.0
            avg_unloading = (m['unloading_time'] / m['unloading_count']) if m['unloading_count'] else 0.0
            avg_disembark = (m['disembarking_time'] / m['disembarking_count']) if m['disembarking_count'] else 0.0
            avg_loading = (m['loading_time'] / m['loading_count']) if m['loading_count'] else 0.0
            safety_pct = (m['safety_done'] / m['safety_total']) * 100.0 if m['safety_total'] else 0.0
            self.airline_summary[code] = {
                'code': code,
                'name': m['name'],
                'flights': m['flights'],
                'avgTurnaround': avg_turnaround,
                'avgGPU': avg_gpu,
                'avgACU': avg_acu,
                'safetyPct': safety_pct,
                'avgCleaning': avg_cleaning,
                'avgBoarding': avg_boarding,
                'avgUnloading': avg_unloading,
                'avgDisembark': avg_disembark,
                'avgLoading': avg_loading,
            }
            # Determine highlights
            # Best performer: lowest avg turnaround among airlines with at least one flight
            if m['flights'] > 0:
                if best is None or avg_turnaround < best['value']:
                    best = {'airline': m['name'], 'value': avg_turnaround}
                if worst is None or avg_turnaround > worst['value']:
                    worst = {'airline': m['name'], 'value': avg_turnaround}
            # Longest GPU usage
            if m['gpu_count'] > 0:
                if longest_gpu is None or avg_gpu > longest_gpu['value']:
                    longest_gpu = {'airline': m['name'], 'value': avg_gpu}
            # Fastest boarding
            if m['boarding_count'] > 0:
                if fastest_boarding is None or avg_boarding < fastest_boarding['value']:
                    fastest_boarding = {'airline': m['name'], 'value': avg_boarding}
        self.airline_highlights = {
            'best': best or {'airline': '-', 'value': 0.0},
            'worst': worst or {'airline': '-', 'value': 0.0},
            'longest_gpu': longest_gpu or {'airline': '-', 'value': 0.0},
            'fastest_boarding': fastest_boarding or {'airline': '-', 'value': 0.0},
        }

    # ---------------------------------------------------------------------
    # UI update helpers
    # ---------------------------------------------------------------------
    def update_data(self) -> None:
        """
        Recompute metrics based on the selected period and date and
        refresh all sections (Services, Checklists, Turnaround,
        Airlines and Reports).  This method should be called whenever
        the period type or date changes or when a different service is
        selected.
        """
        self.compute_data()
        self.update_service_view()
        self.update_checklist_view()
        self.update_turnaround_view()
        self.update_airlines_view()
        self.update_reports_view()

    def update_service_view(self) -> None:
        """Refresh the Services tab to reflect the selected service and timeframe."""
        # Update button colours
        for btn in self.service_buttons:
            svc = btn.cget('text')
            if svc == self.selected_service.get():
                btn.configure(bg="#005bb5")
            else:
                btn.configure(bg="#007bff")
        # Clear frames
        for child in self.services_kpi_frame.winfo_children():
            child.destroy()
        for child in self.services_table_container.winfo_children():
            child.destroy()
        for child in self.services_summary_container.winfo_children():
            child.destroy()
        service = self.selected_service.get()
        # Build KPI cards for the selected service
        cards = []
        if service in ('GPU', 'ACU'):
            totals = self.services_totals[service]
            count = totals['count']
            total_time = totals['time']
            avg_time = (total_time / count) if count else 0.0
            min_time = totals['min'] if totals['min'] != float('inf') else 0.0
            max_time = totals['max']
            # Determine top and least airlines by usage
            airline_counts = totals['airline_counts']
            top_airline = '-' if not airline_counts else max(airline_counts.items(), key=lambda x: x[1])[0]
            least_airline = '-' if not airline_counts else min(airline_counts.items(), key=lambda x: x[1])[0]
            # Convert codes to names if available
            if top_airline != '-':
                top_airline_name = self.airline_settings.get(top_airline, {}).get('name', top_airline)
            else:
                top_airline_name = '-'
            if least_airline != '-':
                least_airline_name = self.airline_settings.get(least_airline, {}).get('name', least_airline)
            else:
                least_airline_name = '-'
            cards = [
                ("Total flights", f"{count}", "#007bff" if service == 'GPU' else "#17a2b8"),
                ("Total time", f"{total_time:.1f} min", "#6f42c1"),
                ("Average", f"{avg_time:.1f} min", "#20c997"),
                ("Shortest", f"{min_time:.1f} min", "#ffc107"),
                ("Longest", f"{max_time:.1f} min", "#dc3545"),
                ("Top airline", top_airline_name, "#28a745"),
                ("Least airline", least_airline_name, "#17a2b8"),
            ]
        else:  # Toilet or Water
            totals = self.services_totals[service]
            count = totals['count']
            airline_counts = totals['airline_counts']
            top_airline = '-' if not airline_counts else max(airline_counts.items(), key=lambda x: x[1])[0]
            least_airline = '-' if not airline_counts else min(airline_counts.items(), key=lambda x: x[1])[0]
            top_name = self.airline_settings.get(top_airline, {}).get('name', top_airline) if top_airline != '-' else '-'
            least_name = self.airline_settings.get(least_airline, {}).get('name', least_airline) if least_airline != '-' else '-'
            cards = [
                ("Total requests", f"{count}", "#007bff" if service == 'Toilet' else "#17a2b8"),
                ("Top airline", top_name, "#28a745"),
                ("Least airline", least_name, "#dc3545"),
            ]
        self.create_kpi_cards(self.services_kpi_frame, cards)
        # Table columns and rows
        if service in ('GPU', 'ACU'):
            columns = ['Flight', 'Airline', 'Start', 'Finish', 'Duration']
            rows = self.services_flight_rows[service][:]
            col_widths = {
                'Flight': 140, 'Airline': 120, 'Start': 140, 'Finish': 140, 'Duration': 90
            }
        else:
            columns = ['Flight', 'Airline', 'Requested']
            rows = self.services_flight_rows[service][:]
            col_widths = {'Flight': 140, 'Airline': 120, 'Requested': 80}
        # Build table
        table_frame = tk.Frame(self.services_table_container, bg="#eef5ff")
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.build_table(table_frame, columns, rows, col_widths)
        # Summary by airline table
        summary_rows = []
        if service in ('GPU', 'ACU'):
            totals = self.services_totals[service]
            for code, cnt in sorted(totals['airline_counts'].items()):
                name = self.airline_settings.get(code, {}).get('name', code)
                avg_time = (totals['airline_times'][code] / cnt) if cnt else 0.0
                summary_rows.append([code, name, cnt, f"{avg_time:.1f}"])
            summary_columns = ['Code', 'Airline', 'Flights', 'Avg Time']
            col_widths2 = {'Code': 80, 'Airline': 120, 'Flights': 80, 'Avg Time': 90}
        else:
            totals = self.services_totals[service]
            for code, cnt in sorted(totals['airline_counts'].items()):
                name = self.airline_settings.get(code, {}).get('name', code)
                summary_rows.append([code, name, cnt])
            summary_columns = ['Code', 'Airline', 'Requests']
            col_widths2 = {'Code': 80, 'Airline': 120, 'Requests': 80}
        summary_frame = tk.Frame(self.services_summary_container, bg="#eef5ff")
        summary_frame.pack(fill=tk.BOTH, expand=True)
        self.build_table(summary_frame, summary_columns, summary_rows, col_widths2)

    def update_checklist_view(self) -> None:
        """Refresh the Checklists tab."""
        # Clear existing widgets in the checklist tab
        for child in self.checklist_content.winfo_children():
            child.destroy()
        # Build KPI cards for overall checklist completion percentages
        cards = [
            ("Chocks", f"{self.checklist_summary.get('chocks', 0.0):.1f}%", "#007bff"),
            ("Cones", f"{self.checklist_summary.get('cones', 0.0):.1f}%", "#17a2b8"),
            ("FOD", f"{self.checklist_summary.get('fod', 0.0):.1f}%", "#28a745"),
            ("Security", f"{self.checklist_summary.get('security', 0.0):.1f}%", "#20c997"),
            ("Toilet", f"{self.checklist_summary.get('toilet', 0.0):.1f}%", "#ffc107"),
            ("Water", f"{self.checklist_summary.get('water', 0.0):.1f}%", "#dc3545"),
        ]
        self.create_kpi_cards(self.checklist_content, cards)
        # Build table of per‑flight checklist statuses
        columns = ['Flight', 'Airline', 'Chocks', 'Cones', 'FOD', 'Security', 'Toilet', 'Water', 'Door Open', 'Door Close']
        rows = []
        for rec in getattr(self, 'filtered_records', self.flight_records_all):
            rows.append([
                rec['flight'], rec['airline_name'],
                'Yes' if rec['chocks'] else 'No',
                'Yes' if rec['cones'] else 'No',
                'Yes' if rec['fod'] else 'No',
                'Yes' if rec['security'] else 'No',
                'Yes' if rec['toilet'] else 'No',
                'Yes' if rec['water'] else 'No',
                rec['doorOpen'], rec['doorClose'],
            ])
        col_widths = {
            'Flight': 140, 'Airline': 120, 'Chocks': 70, 'Cones': 70, 'FOD': 70, 'Security': 80,
            'Toilet': 70, 'Water': 70, 'Door Open': 140, 'Door Close': 140
        }
        table_frame = tk.Frame(self.checklist_content, bg="#eef5ff")
        table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 5))
        self.build_table(table_frame, columns, rows, col_widths)
        # Build summary by airline table
        summary_rows = []
        for code, data in sorted(getattr(self, 'checklist_by_airline', {}).items(), key=lambda x: x[0]):
            summary_rows.append([
                code,
                data['name'],
                data['flights'],
                f"{data['chocks']:.1f}%",
                f"{data['cones']:.1f}%",
                f"{data['fod']:.1f}%",
                f"{data['security']:.1f}%",
                f"{data['toilet']:.1f}%",
                f"{data['water']:.1f}%",
            ])
        summary_columns = ['Code', 'Airline', 'Flights', 'Chocks %', 'Cones %', 'FOD %', 'Security %', 'Toilet %', 'Water %']
        col_widths_sum = {
            'Code': 80, 'Airline': 120, 'Flights': 60, 'Chocks %': 80, 'Cones %': 80,
            'FOD %': 80, 'Security %': 90, 'Toilet %': 80, 'Water %': 80
        }
        summary_frame = tk.Frame(self.checklist_content, bg="#eef5ff")
        summary_frame.pack(fill=tk.BOTH, expand=False, padx=5, pady=(0, 10))
        self.build_table(summary_frame, summary_columns, summary_rows, col_widths_sum)

    def update_turnaround_view(self) -> None:
        """Refresh the Turnaround tab."""
        # Clear existing widgets
        for child in self.turnaround_content.winfo_children():
            child.destroy()
        # KPI cards for turnaround performance
        avg_turn = self.turnaround_summary['avg']
        fastest = self.turnaround_summary['min']
        slowest = self.turnaround_summary['max']
        pct_exceed = self.turnaround_summary['pct_exceed']
        cards = [
            ("Avg Turnaround", f"{avg_turn:.1f} min", "#007bff"),
            ("Fastest", f"{fastest:.1f} min", "#28a745"),
            ("Slowest", f"{slowest:.1f} min", "#dc3545"),
            (">45 min", f"{pct_exceed:.1f}%", "#17a2b8"),
        ]
        # Include averages for individual operations
        for op in ['unloading', 'disembarking', 'cleaning', 'loading', 'boarding']:
            avg_op = self.turnaround_summary['ops_avg'][op]
            label = f"Avg {op.capitalize()}"
            cards.append((label, f"{avg_op:.1f} min", "#6f42c1"))
        self.create_kpi_cards(self.turnaround_content, cards)
        # Per‑flight table
        columns = ['Flight', 'Airline', 'Door Open', 'Door Close', 'Turnaround', 'Unload', 'Disembark', 'Clean', 'Load', 'Board']
        rows = []
        for rec in getattr(self, 'filtered_records', self.flight_records_all):
            rows.append([
                rec['flight'], rec['airline_name'], rec['doorOpen'], rec['doorClose'],
                f"{rec['turnaroundDuration']:.1f}" if rec['turnaroundDuration'] is not None else '',
                f"{rec['unloadingDuration']:.1f}" if rec['unloadingDuration'] is not None else '',
                f"{rec['disembarkingDuration']:.1f}" if rec['disembarkingDuration'] is not None else '',
                f"{rec['cleaningDuration']:.1f}" if rec['cleaningDuration'] is not None else '',
                f"{rec['loadingDuration']:.1f}" if rec['loadingDuration'] is not None else '',
                f"{rec['boardingDuration']:.1f}" if rec['boardingDuration'] is not None else '',
            ])
        col_widths = {
            'Flight': 140, 'Airline': 120, 'Door Open': 140, 'Door Close': 140, 'Turnaround': 110,
            'Unload': 90, 'Disembark': 110, 'Clean': 90, 'Load': 90, 'Board': 90
        }
        table_frame = tk.Frame(self.turnaround_content, bg="#eef5ff")
        table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 5))
        self.build_table(table_frame, columns, rows, col_widths)
        # Summary by airline table: show flights and averages for turnaround and sub operations
        summary_rows = []
        for code, data in sorted(getattr(self, 'airline_summary', {}).items(), key=lambda x: x[0]):
            summary_rows.append([
                code,
                data['name'],
                data['flights'],
                f"{data['avgTurnaround']:.1f}",
                f"{data['avgUnloading']:.1f}",
                f"{data['avgDisembark']:.1f}",
                f"{data['avgCleaning']:.1f}",
                f"{data['avgLoading']:.1f}",
                f"{data['avgBoarding']:.1f}",
            ])
        summary_columns = ['Code', 'Airline', 'Flights', 'Avg Turn', 'Avg Unload', 'Avg Disembark', 'Avg Clean', 'Avg Load', 'Avg Board']
        col_widths_sum = {
            'Code': 80, 'Airline': 120, 'Flights': 60, 'Avg Turn': 100, 'Avg Unload': 90,
            'Avg Disembark': 110, 'Avg Clean': 90, 'Avg Load': 90, 'Avg Board': 90
        }
        summary_frame = tk.Frame(self.turnaround_content, bg="#eef5ff")
        summary_frame.pack(fill=tk.BOTH, expand=False, padx=5, pady=(0, 10))
        self.build_table(summary_frame, summary_columns, summary_rows, col_widths_sum)

    def update_airlines_view(self) -> None:
        """Refresh the Airlines tab."""
        for child in self.airline_content.winfo_children():
            child.destroy()
        # Highlights cards
        best = self.airline_highlights['best']
        worst = self.airline_highlights['worst']
        longest_gpu = self.airline_highlights['longest_gpu']
        fastest_board = self.airline_highlights['fastest_boarding']
        cards = [
            ("Best performer", f"{best['airline']} ({best['value']:.1f} min)", "#28a745"),
            ("Worst performer", f"{worst['airline']} ({worst['value']:.1f} min)", "#dc3545"),
            ("Longest GPU", f"{longest_gpu['airline']} ({longest_gpu['value']:.1f} min)", "#17a2b8"),
            ("Fastest boarding", f"{fastest_board['airline']} ({fastest_board['value']:.1f} min)", "#ffc107"),
        ]
        self.create_kpi_cards(self.airline_content, cards)
        # Build table of airline metrics
        columns = ['Code', 'Airline', 'Flights', 'Avg Turnaround', 'Avg GPU', 'Avg ACU', 'Safety %', 'Avg Clean', 'Avg Board', 'Avg Unload', 'Avg Disembark', 'Avg Load']
        rows = []
        for code, data in sorted(self.airline_summary.items(), key=lambda x: x[0]):
            rows.append([
                code, data['name'], data['flights'], f"{data['avgTurnaround']:.1f}", f"{data['avgGPU']:.1f}", f"{data['avgACU']:.1f}",
                f"{data['safetyPct']:.1f}%", f"{data['avgCleaning']:.1f}", f"{data['avgBoarding']:.1f}", f"{data['avgUnloading']:.1f}", f"{data['avgDisembark']:.1f}", f"{data['avgLoading']:.1f}"
            ])
        col_widths = {
            'Code': 80, 'Airline': 140, 'Flights': 60, 'Avg Turnaround': 120, 'Avg GPU': 90, 'Avg ACU': 90,
            'Safety %': 90, 'Avg Clean': 90, 'Avg Board': 90, 'Avg Unload': 90, 'Avg Disembark': 110, 'Avg Load': 90
        }
        table_frame = tk.Frame(self.airline_content, bg="#eef5ff")
        table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 10))
        self.build_table(table_frame, columns, rows, col_widths)

    def update_reports_view(self) -> None:
        """
        Refresh the Reports tab.  This section displays any notes or
        remarks sent from the Turnaround app (stored under the 'remarks'
        field for each flight).  It shows a count of total reports and
        a table listing flights with remarks for the current period.
        """
        # Clear existing content
        for child in self.reports_content.winfo_children():
            child.destroy()
        # Gather reports from filtered flights
        rows = []
        for rec in getattr(self, 'filtered_records', self.flight_records_all):
            remarks = rec.get('remarks', '').strip()
            if remarks:
                rows.append([
                    rec['flight'], rec['airline_name'],
                    rec.get('date').strftime("%Y-%m-%d") if rec.get('date') else '',
                    remarks
                ])
        # KPI cards: total number of reports
        total_reports = len(rows)
        cards = [
            ("Total Reports", str(total_reports), "#dc3545" if total_reports else "#28a745"),
        ]
        self.create_kpi_cards(self.reports_content, cards)
        # Build table if there are any reports
        if rows:
            columns = ['Flight', 'Airline', 'Date', 'Remarks']
            # Limit remarks width in the table by truncating if necessary
            formatted_rows = []
            for r in rows:
                # Optionally truncate long remarks for table display
                remark = r[3]
                # Show first 80 chars; full text is available in table cell
                if len(remark) > 120:
                    short = remark[:117] + '...'
                else:
                    short = remark
                formatted_rows.append([r[0], r[1], r[2], short])
            col_widths = {'Flight': 140, 'Airline': 140, 'Date': 100, 'Remarks': 400}
            table_frame = tk.Frame(self.reports_content, bg="#eef5ff")
            table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 10))
            self.build_table(table_frame, columns, formatted_rows, col_widths)

    # ---------------------------------------------------------------------
    # Shared UI helpers
    # ---------------------------------------------------------------------
    def create_kpi_cards(self, parent: tk.Frame, kpis: list) -> None:
        """
        Create a row of KPI cards within the given parent frame.  Each entry
        in the kpis list should be a tuple (title, value, colour).
        """
        card_frame = tk.Frame(parent, bg="#eef5ff")
        card_frame.pack(fill=tk.X, pady=(5, 5))
        for title, value, colour in kpis:
            cf = tk.Frame(card_frame, bg=colour, padx=10, pady=8)
            cf.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
            tk.Label(cf, text=title, fg="#ffffff", bg=colour, font=("Arial", 9, "bold")).pack(anchor='w')
            tk.Label(cf, text=value, fg="#ffffff", bg=colour, font=("Arial", 14, "bold")).pack(anchor='w')

    def build_table(self, parent: tk.Frame, columns: list, rows: list, col_widths: Dict[str, int]) -> ttk.Treeview:
        """
        Create a sortable Treeview table with the given columns and populate it
        with the provided rows.  Column widths are defined via the
        col_widths dictionary.  Returns the Treeview instance.
        """
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure(
            "Stats.Treeview",
            background="#ffffff",
            foreground="#333333",
            fieldbackground="#ffffff",
            rowheight=22,
            font=("Arial", 9)
        )
        style.configure(
            "Stats.Treeview.Heading",
            background="#003366",
            foreground="#ffffff",
            font=("Arial", 9, "bold")
        )
        style.map("Stats.Treeview", background=[('selected', '#b3d7ff')])
        tree = ttk.Treeview(parent, columns=columns, show='headings', style="Stats.Treeview")
        # Determine index mapping
        col_index = {c: i for i, c in enumerate(columns)}
        # Setup headings with sortable callback
        sort_states = {c: False for c in columns}
        def sort_key(col, val):
            # Try to parse numeric values
            try:
                # Duration / numeric columns
                if val in ('', None):
                    return 0.0
                return float(val) if any(keyword in col.lower() for keyword in ['duration', 'avg', 'time']) else val
            except Exception:
                # Dates and times parse
                try:
                    return pd.to_datetime(val)
                except Exception:
                    return val
        def sort_by(col):
            reverse = sort_states[col]
            sort_states[col] = not reverse
            rows.sort(key=lambda r: sort_key(col, r[col_index[col]]), reverse=reverse)
            # Update headings
            for c in columns:
                arrow = ''
                if c == col:
                    arrow = ' \u25BC' if reverse else ' \u25B2'
                tree.heading(c, text=c, command=lambda c=c: sort_by(c))
            refresh()
        for c in columns:
            tree.heading(c, text=c, command=lambda c=c: sort_by(c))
            w = col_widths.get(c, 100)
            tree.column(c, width=w, anchor='w')
        # Scrollbars
        vsb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        def refresh():
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert('', tk.END, values=r)
        refresh()
        return tree


# ----------------------- Settings Window -----------------------
class SettingsWindow(tk.Toplevel):
    """
    Modern window for managing airline settings.  This window presents
    a two‑column layout: a list of airlines on the left and a form
    for editing details on the right.  Users can add or edit airline
    codes, names, instructions and layout URLs, as well as define
    aircraft‑specific instructions.  Changes are saved back to the
    parent CoordinationApp's airline_settings and persisted to disk via
    save_airline_settings().  Uploading of instructions to Firebase
    occurs when saving.

    Parameters
    ----------
    parent : CoordinationApp
        The coordination application instance.  Used for saving and
        persisting settings.
    airline_settings : dict
        Mutable dictionary of airline settings to edit.
    """
    def __init__(self, parent: 'CoordinationApp', airline_settings: Dict[str, Dict[str, str]]) -> None:
        super().__init__(parent.root)
        self.parent = parent
        # Use the passed dictionary directly so edits reflect in the parent
        self.airline_settings = airline_settings
        self.title("Settings")
        self.geometry("900x600")
        # Set a cohesive background colour
        try:
            self.configure(bg="#eef5ff")
        except Exception:
            pass
        # Internal state variables
        self.code_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.type_var = tk.StringVar()
        self.layout_var = tk.StringVar()
        # Build UI
        self.create_widgets()
        # Populate airlines list
        self.refresh_airline_list()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    def create_widgets(self) -> None:
        """Assemble the components of the settings window."""
        # Left panel for airline list
        left_frame = tk.Frame(self, bg="#eef5ff")
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)
        tk.Label(left_frame, text="Airlines", bg="#eef5ff", fg="#003366", font=("Arial", 10, "bold")).pack(anchor='w')
        # Treeview for airline codes and names
        cols = ('Code', 'Name')
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure(
            "Settings.Treeview",
            background="#ffffff",
            foreground="#333333",
            fieldbackground="#ffffff",
            rowheight=22,
            font=("Arial", 9)
        )
        style.configure(
            "Settings.Treeview.Heading",
            background="#003366",
            foreground="#ffffff",
            font=("Arial", 9, "bold")
        )
        style.map("Settings.Treeview", background=[('selected', '#b3d7ff')])
        self.airline_tree = ttk.Treeview(left_frame, columns=cols, show='headings', style="Settings.Treeview", selectmode='browse', height=20)
        for c, w in [('Code', 80), ('Name', 140)]:
            self.airline_tree.heading(c, text=c)
            self.airline_tree.column(c, width=w, anchor='w')
        yscroll = ttk.Scrollbar(left_frame, orient='vertical', command=self.airline_tree.yview)
        self.airline_tree.configure(yscrollcommand=yscroll.set)
        self.airline_tree.pack(side=tk.LEFT, fill=tk.Y, expand=False)
        yscroll.pack(side=tk.LEFT, fill=tk.Y)
        # Bind selection
        self.airline_tree.bind('<<TreeviewSelect>>', self.load_selected_airline)
        # Right panel for editing
        right_frame = tk.Frame(self, bg="#eef5ff")
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        # General details group
        general_frame = tk.LabelFrame(right_frame, text="General", bg="#eef5ff", fg="#003366", font=("Arial", 10, "bold"))
        general_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(general_frame, text="Code:", bg="#eef5ff", fg="#003366").grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.code_entry = tk.Entry(general_frame, textvariable=self.code_var)
        self.code_entry.grid(row=0, column=1, sticky='w', padx=5, pady=2)
        tk.Label(general_frame, text="Airline Name:", bg="#eef5ff", fg="#003366").grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.name_entry = tk.Entry(general_frame, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=1, column=1, sticky='w', padx=5, pady=2)
        # Instructions and layout (general)
        tk.Label(general_frame, text="Instructions:", bg="#eef5ff", fg="#003366").grid(row=2, column=0, sticky='ne', padx=5, pady=2)
        self.instr_text = tk.Text(general_frame, width=50, height=5)
        self.instr_text.grid(row=2, column=1, sticky='w', padx=5, pady=2)
        tk.Label(general_frame, text="Layout URL:", bg="#eef5ff", fg="#003366").grid(row=3, column=0, sticky='e', padx=5, pady=2)
        self.layout_entry = tk.Entry(general_frame, textvariable=self.layout_var, width=50)
        self.layout_entry.grid(row=3, column=1, sticky='w', padx=5, pady=2)
        # Types group
        types_frame = tk.LabelFrame(right_frame, text="Aircraft Types", bg="#eef5ff", fg="#003366", font=("Arial", 10, "bold"))
        types_frame.pack(fill=tk.BOTH, expand=True)
        # List of types
        tk.Label(types_frame, text="Types", bg="#eef5ff", fg="#003366").grid(row=0, column=0, sticky='w', padx=5, pady=2)
        self.type_list = tk.Listbox(types_frame, height=5)
        self.type_list.grid(row=1, column=0, sticky='nw', padx=5, pady=2)
        self.type_list.bind('<<ListboxSelect>>', self.load_selected_type)
        # Type entry and details
        tk.Label(types_frame, text="Selected Type:", bg="#eef5ff", fg="#003366").grid(row=0, column=1, sticky='e', padx=5, pady=2)
        self.type_entry = tk.Entry(types_frame, textvariable=self.type_var, width=20)
        self.type_entry.grid(row=0, column=2, sticky='w', padx=5, pady=2)
        tk.Label(types_frame, text="Type Instructions:", bg="#eef5ff", fg="#003366").grid(row=2, column=1, sticky='ne', padx=5, pady=2)
        self.type_instr_text = tk.Text(types_frame, width=40, height=4)
        self.type_instr_text.grid(row=2, column=2, sticky='w', padx=5, pady=2)
        tk.Label(types_frame, text="Type Layout URL:", bg="#eef5ff", fg="#003366").grid(row=3, column=1, sticky='e', padx=5, pady=2)
        self.type_layout_entry = tk.Entry(types_frame, width=40)
        self.type_layout_entry.grid(row=3, column=2, sticky='w', padx=5, pady=2)
        # Buttons
        btn_frame = tk.Frame(right_frame, bg="#eef5ff")
        btn_frame.pack(fill=tk.X, pady=10)
        tk.Button(btn_frame, text="Save", command=self.save_mapping, bg="#007bff", fg="#ffffff").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Delete", command=self.delete_mapping, bg="#dc3545", fg="#ffffff").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Close", command=self.destroy, bg="#6c757d", fg="#ffffff").pack(side=tk.LEFT, padx=5)

    # ------------------------------------------------------------------
    # Data loading and refreshing
    # ------------------------------------------------------------------
    def refresh_airline_list(self) -> None:
        """Populate the airline list from the current airline_settings."""
        # Clear tree
        for i in self.airline_tree.get_children():
            self.airline_tree.delete(i)
        # Insert entries sorted by code
        for code in sorted(self.airline_settings.keys()):
            name = self.airline_settings.get(code, {}).get('name', '')
            self.airline_tree.insert('', tk.END, iid=code, values=(code, name))

    def refresh_type_list(self, code: str) -> None:
        """Refresh the list of aircraft types for the given airline code."""
        self.type_list.delete(0, tk.END)
        # Always include blank entry for general instructions
        self.type_list.insert(tk.END, "")
        data = self.airline_settings.get(code, {})
        for t in sorted(data.get('types', {}).keys()):
            self.type_list.insert(tk.END, t)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def load_selected_airline(self, event=None) -> None:
        """Load the selected airline's details into the form."""
        selected = self.airline_tree.selection()
        if not selected:
            return
        code = selected[0]
        # Set code and name
        self.code_var.set(code)
        data = self.airline_settings.get(code, {})
        self.name_var.set(data.get('name', ''))
        # General instructions and layout
        self.instr_text.delete('1.0', tk.END)
        self.instr_text.insert(tk.END, data.get('instructions', ''))
        self.layout_var.set(data.get('layoutUrl', ''))
        # Refresh type list and reset type fields
        self.refresh_type_list(code)
        self.type_var.set('')
        self.type_entry.delete(0, tk.END)
        self.type_instr_text.delete('1.0', tk.END)
        self.type_layout_entry.delete(0, tk.END)

    def load_selected_type(self, event=None) -> None:
        """Load the selected aircraft type's instructions and layout."""
        sel = self.type_list.curselection()
        if not sel:
            return
        idx = sel[0]
        t = self.type_list.get(idx)
        self.type_var.set(t)
        # Clear entry field and set selected type
        self.type_entry.delete(0, tk.END)
        self.type_entry.insert(0, t)
        # Retrieve airline code
        c = self.code_var.get().strip().upper()
        data = self.airline_settings.get(c, {})
        if t == "":
            # General instructions
            self.type_instr_text.delete('1.0', tk.END)
            self.type_layout_entry.delete(0, tk.END)
        else:
            type_data = data.get('types', {}).get(t, {})
            self.type_instr_text.delete('1.0', tk.END)
            self.type_instr_text.insert(tk.END, type_data.get('instructions', ''))
            self.type_layout_entry.delete(0, tk.END)
            self.type_layout_entry.insert(0, type_data.get('layoutUrl', ''))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def save_mapping(self) -> None:
        """Persist current entries to airline_settings and update views."""
        c = self.code_var.get().strip().upper()
        if not c:
            messagebox.showwarning("Validation", "Code cannot be empty.")
            return
        if c not in self.airline_settings:
            self.airline_settings[c] = {'name': '', 'instructions': '', 'layoutUrl': '', 'types': {}}
        # Update name
        self.airline_settings[c]['name'] = self.name_var.get().strip()
        # General instructions and layout
        self.airline_settings[c]['instructions'] = self.instr_text.get('1.0', tk.END).strip()
        self.airline_settings[c]['layoutUrl'] = self.layout_var.get().strip()
        # Determine selected type and update accordingly
        t = self.type_entry.get().strip().upper()
        # Normalize numeric types (remove trailing .0)
        try:
            if t and any(ch.isdigit() for ch in t) and '.' in t:
                fval = float(t)
                if fval.is_integer():
                    t = str(int(fval)).upper()
        except Exception:
            pass
        if t:
            instr = self.type_instr_text.get('1.0', tk.END).strip()
            layout = self.type_layout_entry.get().strip()
            if 'types' not in self.airline_settings[c]:
                self.airline_settings[c]['types'] = {}
            self.airline_settings[c]['types'][t] = {'instructions': instr, 'layoutUrl': layout}
        # Refresh lists
        self.refresh_airline_list()
        self.airline_tree.selection_set(c)
        self.refresh_type_list(c)
        # Persist settings to disk and upload to Firebase via parent's save method
        self.parent.save_airline_settings()
        messagebox.showinfo("Saved", f"Settings for {c} saved.")

    def delete_mapping(self) -> None:
        """Delete the selected airline or aircraft type from settings."""
        c = self.code_var.get().strip().upper()
        if not c or c not in self.airline_settings:
            return
        t = self.type_entry.get().strip().upper()
        if not t:
            # Delete entire airline mapping
            if messagebox.askyesno("Delete", f"Delete all settings for {c}?"):
                del self.airline_settings[c]
                self.refresh_airline_list()
                # Clear forms
                self.code_var.set('')
                self.name_var.set('')
                self.instr_text.delete('1.0', tk.END)
                self.layout_var.set('')
                self.type_list.delete(0, tk.END)
                self.type_entry.delete(0, tk.END)
                self.type_instr_text.delete('1.0', tk.END)
                self.type_layout_entry.delete(0, tk.END)
                # Persist changes to file and upload to Firebase
                self.parent.save_airline_settings()
        else:
            # Delete selected type
            if messagebox.askyesno("Delete", f"Delete type {t} for {c}?"):
                self.airline_settings[c].get('types', {}).pop(t, None)
                self.refresh_type_list(c)
                # Clear type fields
                self.type_var.set('')
                self.type_entry.delete(0, tk.END)
                self.type_instr_text.delete('1.0', tk.END)
                self.type_layout_entry.delete(0, tk.END)
                # Persist changes to file and upload to Firebase
                self.parent.save_airline_settings()


if __name__ == "__main__":
    root = tk.Tk()
    app = CoordinationApp(root)
    root.mainloop()
