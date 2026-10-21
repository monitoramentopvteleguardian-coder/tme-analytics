import os, io, json, hashlib, secrets, urllib.request, urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

APP_NAME = "TME Analytics"
# Dados públicos do projeto Supabase fornecidos pelo usuário.
# A chave publishable é apropriada para Data API com políticas RLS; não é senha do banco.
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://bkmyfyegzrzvdmtokjcw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Para persistência real no Supabase, configure DATABASE_URL com a string PostgreSQL
# em st.secrets (Streamlit Cloud) ou variável de ambiente. Sem ela, mantém SQLite local.
def get_secret(name, default=None):
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

DATABASE_URL = get_secret("DATABASE_URL", "sqlite:///./tme_analytics.db")
IMPORT_DIR = Path(os.getenv("IMPORT_DIR", "./imports"))
IMPORT_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

st.set_page_config(page_title=APP_NAME, page_icon="📊", layout="wide")

# ---------- Database ----------
def init_db():
    with engine.begin() as c:
        c.execute(text("""CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(100) PRIMARY KEY,
            password_hash VARCHAR(256) NOT NULL, full_name VARCHAR(200) NOT NULL,
            role VARCHAR(30) NOT NULL DEFAULT 'Operador', team VARCHAR(150),
            active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""))
        c.execute(text("""CREATE TABLE IF NOT EXISTS call_records (
            event_date DATE NOT NULL, team VARCHAR(150),
            operator VARCHAR(150), total_calls INTEGER DEFAULT 0, answered INTEGER DEFAULT 0,
            abandoned INTEGER DEFAULT 0, tme_seconds FLOAT DEFAULT 0,
            source_file VARCHAR(500), imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(event_date, team, operator, source_file)
        )"""))
        c.execute(text("""CREATE TABLE IF NOT EXISTS settings (
            key VARCHAR(100) PRIMARY KEY, value VARCHAR(1000) NOT NULL
        )"""))
        c.execute(text("""CREATE TABLE IF NOT EXISTS audit_log (
            username VARCHAR(100), action VARCHAR(300),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""))
        c.execute(text("INSERT OR IGNORE INTO settings(key,value) VALUES ('tme_target_seconds','07')") if DATABASE_URL.startswith("sqlite") else text("INSERT INTO settings(key,value) VALUES ('tme_target_seconds','07') ON CONFLICT (key) DO NOTHING"))
        # Migra a meta antiga padrão de 30s para a nova meta solicitada de 7s.
        c.execute(text("UPDATE settings SET value='7' WHERE key='tme_target_seconds' AND value='30'"))
        count = c.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        if count == 0:
            initial_password = get_secret("INITIAL_ADMIN_PASSWORD", "")
            if not initial_password or len(initial_password) < 12:
                st.error("Configuração inicial necessária: defina INITIAL_ADMIN_PASSWORD nos Secrets do Streamlit (mínimo de 12 caracteres) para criar o administrador inicial.")
                st.stop()
            c.execute(text("INSERT INTO users(username,password_hash,full_name,role,team) VALUES (:u,:p,:n,:r,:t)"),
                      {"u":"admin", "p":hash_password(initial_password), "n":"Administrador", "r":"Administrador", "t":"Geral"})

def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return f"{salt}${digest}"

def verify_password(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        test_digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
        return secrets.compare_digest(digest, test_digest)
    except Exception:
        return False

def audit(user, action):
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_log(username,action) VALUES (:u,:a)"), {"u":user, "a":action})

def read_df(query, params=None):
    with engine.connect() as c:
        return pd.read_sql(text(query), c, params=params or {})

def get_target():
    with engine.connect() as c:
        row = c.execute(text("SELECT value FROM settings WHERE key='tme_target_seconds'")).first()
        return float(row[0]) if row else 7.0

def set_target(value):
    with engine.begin() as c:
        if DATABASE_URL.startswith("sqlite"):
            c.execute(text("UPDATE settings SET value=:v WHERE key='tme_target_seconds'"), {"v":str(value)})
            c.execute(text("INSERT OR IGNORE INTO settings(key,value) VALUES ('tme_target_seconds',:v)"), {"v":str(value)})
        else:
            c.execute(text("INSERT INTO settings(key,value) VALUES ('tme_target_seconds',:v) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"), {"v":str(value)})

init_db()

# ---------- Import helpers ----------
def normalize_columns(df):
    aliases = {
        "data":"date", "data/hora":"date", "date":"date", "datetime":"date",
        "equipe":"team", "team":"team", "fila":"team", "operador":"operator",
        "agente":"operator", "operator":"operator", "agent":"operator",
        "chamadas":"total_calls", "chamadas na fila":"total_calls", "total de chamadas":"total_calls", "total_calls":"total_calls",
        "atendidas":"answered", "chamadas atendidas":"answered", "answered":"answered",
        "abandonadas":"abandoned", "chamadas abandonadas":"abandoned", "abandoned":"abandoned",
        "tme":"tme_seconds", "tme (segundos)":"tme_seconds", "tme do dia (segundos)":"tme_seconds",
        "tme_seconds":"tme_seconds", "tempo médio de espera (segundos)":"tme_seconds",
        "tempo medio de espera (segundos)":"tme_seconds",
    }
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    # Formato de eventos exportado pelo portal de telefonia.
    # Mantém o mapeamento de códigos usado pela versão anterior:
    # 11 = entrada na fila; 10 = atendida/conexão; 8 = abandonada.
    if {"datetime", "qevent"}.issubset(set(df.columns)):
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["qevent"] = pd.to_numeric(df["qevent"], errors="coerce")
        for col in ("info1", "qname", "qagent"):
            if col not in df.columns:
                df[col] = 0 if col == "info1" else "Não identificado"
        df["info1"] = pd.to_numeric(df["info1"], errors="coerce").fillna(0)
        df["date"] = df["datetime"].dt.date
        df = df[df["date"].notna()].copy()
        # Preserva fila e agente, em vez de juntar tudo em "Geral".
        out = []
        for (d, queue, agent), g in df.groupby(["date", "qname", "qagent"], dropna=False):
            queued = int((g["qevent"] == 11).sum())
            answered = int((g["qevent"] == 10).sum())
            abandoned = int((g["qevent"] == 8).sum())
            waits = g.loc[g["qevent"] == 10, "info1"]
            out.append({
                "date": d,
                "team": str(queue) if pd.notna(queue) else "Não identificado",
                "operator": str(agent) if pd.notna(agent) else "Não identificado",
                "total_calls": max(queued, answered + abandoned),
                "answered": answered,
                "abandoned": abandoned,
                "tme_seconds": float(waits.mean()) if len(waits) else 0.0,
            })
        return pd.DataFrame(out)
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
    if {"datetime", "qevent"}.issubset(set(df.columns)):
        df["date"] = pd.to_datetime(df["datetime"], errors="coerce").dt.date
        df["qevent"] = pd.to_numeric(df["qevent"], errors="coerce")
        df["info1"] = pd.to_numeric(df.get("info1", 0), errors="coerce").fillna(0)
        out = []
        for d, g in df.groupby("date"):
            if pd.isna(d): continue
            queued = int((g.qevent == 11).sum())
            answered = int((g.qevent == 10).sum())
            abandoned = int((g.qevent == 8).sum())
            waits = g.loc[g.qevent == 10, "info1"]
            out.append({"date":d, "team":"Geral", "operator":"Não identificado",
                        "total_calls":max(queued, answered+abandoned), "answered":answered,
                        "abandoned":abandoned, "tme_seconds":float(waits.mean()) if len(waits) else 0})
        return pd.DataFrame(out)
    if "date" not in df.columns:
        raise ValueError("Não encontrei a coluna Data/Date. Inclua também TME, atendidas, abandonadas e, se possível, equipe e operador.")
    df["date"] = pd.to_datetime(df["date"], errors="coerce", dayfirst=True).dt.date
    df = df[df["date"].notna()].copy()
    for col, default in [("team","Geral"),("operator","Não identificado")]:
        if col not in df: df[col] = default
        df[col] = df[col].fillna(default).astype(str)
    for col in ["total_calls","answered","abandoned","tme_seconds"]:
        if col not in df: df[col] = 0
        df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ".", regex=False), errors="coerce").fillna(0)
    # Combine multiple rows per day/team/operator with weighted TME when call counts are present.
    grouped=[]
    for (d,t,o), g in df.groupby(["date","team","operator"], dropna=False):
        n = g["answered"].sum()
        tme = (g["tme_seconds"] * g["answered"]).sum()/n if n > 0 else g["tme_seconds"].mean()
        grouped.append({"date":d,"team":t,"operator":o,"total_calls":int(g["total_calls"].sum()),
                        "answered":int(g["answered"].sum()),"abandoned":int(g["abandoned"].sum()),
                        "tme_seconds":float(tme)})
    return pd.DataFrame(grouped)


def import_dataframe(file_bytes, filename, username):
    if filename.lower().endswith((".xlsx", ".xls")):
        excel = pd.ExcelFile(io.BytesIO(file_bytes))

        # Prioriza a aba que contém os lançamentos diários
        aba_diaria = next(
            (
                aba for aba in excel.sheet_names
                if aba.strip().casefold()
                in ("lançamento diário", "lancamento diario")
            ),
            excel.sheet_names[0]
        )

        raw = pd.read_excel(excel, sheet_name=aba_diaria)
    else:

        try: raw = pd.read_csv(io.BytesIO(file_bytes), sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError: raw = pd.read_csv(io.BytesIO(file_bytes), sep=None, engine="python", encoding="latin-1")
    clean = normalize_columns(raw)
    if clean.empty: raise ValueError("O arquivo não contém linhas válidas.")
    clean["source_file"] = filename
    with engine.begin() as c:
        for r in clean.to_dict("records"):
            # Portable upsert strategy: delete exact matching key, then insert.
            c.execute(text("""DELETE FROM call_records WHERE event_date=:d AND team=:t AND operator=:o AND source_file=:s"""),
                      {"d":r["date"],"t":r["team"],"o":r["operator"],"s":filename})
            c.execute(text("""INSERT INTO call_records(event_date,team,operator,total_calls,answered,abandoned,tme_seconds,source_file)
                VALUES (:d,:t,:o,:tc,:an,:ab,:tme,:s)"""),
                {"d":r["date"],"t":r["team"],"o":r["operator"],"tc":int(r["total_calls"]),
                 "an":int(r["answered"]),"ab":int(r["abandoned"]),"tme":float(r["tme_seconds"]),"s":filename})
    audit(username, f"Importou {filename} ({len(clean)} registros)")
    return len(clean)

def load_records(start_date=None, end_date=None):
    # No Supabase, prioriza os eventos brutos já importados. O evento 10 contém
    # info1 preenchido e, nos exemplos analisados, coincide com o intervalo entre
    # os eventos 11 e 10. Trate este TME como provisório até validar a regra com o fornecedor.
    params = {}
    if not DATABASE_URL.startswith("sqlite"):
        try:
            clauses = ["qevent IN ('11','10','8')", "datetime IS NOT NULL"]
            if start_date:
                clauses.append("CAST(datetime AS TIMESTAMP) >= CAST(:start AS DATE)")
                params["start"] = start_date
            if end_date:
                clauses.append("CAST(datetime AS TIMESTAMP) < CAST(:end AS DATE) + INTERVAL '1 day'")
                params["end"] = end_date
            raw_query = f"""SELECT CAST(CAST(datetime AS TIMESTAMP) AS DATE) AS "Data",
                COALESCE(qname, 'Não identificado') AS "Equipe",
                'Não identificado' AS "Operador",
                COUNT(*) FILTER (WHERE qevent='11') AS "Entradas na fila",
                COUNT(*) FILTER (WHERE qevent='10') AS "Atendidas",
                COUNT(*) FILTER (WHERE qevent='8') AS "Abandonadas",
                AVG(NULLIF(info1, '')::numeric) FILTER (WHERE qevent='10') AS "TME"
                FROM public.queue_events WHERE {' AND '.join(clauses)}
                GROUP BY 1,2 ORDER BY 1"""
            raw = read_df(raw_query, params)
            if not raw.empty:
                raw["Chamadas"] = raw["Entradas na fila"].fillna(0).astype(int)
                raw["Atendidas"] = raw["Atendidas"].fillna(0).astype(int)
                raw["Abandonadas"] = raw["Abandonadas"].fillna(0).astype(int)
                raw["TME"] = pd.to_numeric(raw["TME"], errors="coerce")
                raw["TME"] = raw["TME"].fillna(0.0)
                raw["Data"] = pd.to_datetime(raw["Data"])
                raw["Semana"] = raw["Data"].dt.to_period("W").astype(str)
                return raw[["Data", "Equipe", "Operador", "Chamadas", "Atendidas", "Abandonadas", "TME", "Semana"]]
        except Exception as exc:
            st.error(f"Não foi possível ler public.queue_events no Supabase: {exc}")
            return pd.DataFrame(columns=["Data", "Equipe", "Operador", "Chamadas", "Atendidas", "Abandonadas", "TME", "Semana"])
        # Em produção, a fonte oficial é public.queue_events. Não misturar com
        # call_records, que contém apenas lançamentos/importações feitos no app.
        return pd.DataFrame(columns=["Data", "Equipe", "Operador", "Chamadas", "Atendidas", "Abandonadas", "TME", "Semana"])
    query = "SELECT event_date AS Data, team AS Equipe, operator AS Operador, total_calls AS Chamadas, answered AS Atendidas, abandoned AS Abandonadas, tme_seconds AS TME FROM call_records"
    params={}
    clauses=[]
    if start_date: clauses.append("event_date >= :start"); params["start"]=start_date
    if end_date: clauses.append("event_date <= :end"); params["end"]=end_date
    if clauses: query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY event_date"
    df=read_df(query,params)
    if not df.empty:
        df["Data"]=pd.to_datetime(df["Data"])
        df["Semana"]=df["Data"].dt.to_period("W").astype(str)
    return df

# ---------- Login ----------
if "user" not in st.session_state: st.session_state.user = None
if not st.session_state.user:
    st.title("📊 TME Analytics")
    st.caption("Plataforma de análise de TME e telefonia")
    with st.form("login"):
        username=st.text_input("Usuário")
        password=st.text_input("Senha", type="password")
        submitted=st.form_submit_button("Entrar", use_container_width=True)
    if submitted:
        with engine.connect() as c:
            user=c.execute(text("SELECT username,password_hash,full_name,role,team,active FROM users WHERE username=:u"), {"u":username.strip()}).first()
        if user and user[5] and verify_password(password, user[1]):
            st.session_state.user={"username":user[0],"name":user[2],"role":user[3],"team":user[4]}
            audit(user[0],"Login")
            st.rerun()
        else: st.error("Usuário ou senha inválidos, ou conta inativa.")
    st.info("Primeiro acesso: usuário `admin` e a senha inicial definida nos Secrets do deploy. Após entrar, altere a senha na página Configurações.")
    st.stop()

user=st.session_state.user
is_admin=user["role"] in ("Administrador","Gestor")
st.sidebar.title("🎧 TME Analytics")
st.sidebar.caption(f"{user['name']} · {user['role']}")
page=st.sidebar.radio("Navegação", ["Dashboard","Lançamento manual diário","Telefonia ao vivo (API)","TME Diário","TME Semanal","Importação de dados","Metas e alertas","Relatórios","Equipes e operadores","Usuários e acesso","Configurações"], label_visibility="collapsed")
if st.sidebar.button("Sair", use_container_width=True):
    audit(user["username"],"Logout"); st.session_state.user=None; st.rerun()

st.title(page)
st.caption("Análise de atendimento, tempo médio de espera e desempenho de telefonia")

if page == "Lançamento manual diário":
    st.subheader("Registrar os números gerais da operação")
    st.write("Informe os totais consolidados do dia. Se lançar novamente a mesma data, o registro manual anterior será substituído, evitando duplicidade.")
    manual_source = "Lançamento manual diário"
    with st.form("manual_daily_entry"):
        entry_date = st.date_input("Data do relatório", value=date.today())
        c1, c2, c3, c4 = st.columns(4)
        total_calls = c1.number_input("Total de chamadas", min_value=0, step=1, value=0)
        answered = c2.number_input("Chamadas atendidas", min_value=0, step=1, value=0)
        abandoned = c3.number_input("Chamadas abandonadas", min_value=0, step=1, value=0)
        tme_seconds = c4.number_input("TME do dia (segundos)", min_value=0.0, max_value=86400.0, step=1.0, value=0.0)
        save_manual = st.form_submit_button("Salvar lançamento", type="primary", use_container_width=True)
    if save_manual:
        if answered + abandoned > total_calls:
            st.error("Atendidas + abandonadas não podem ser maiores que o total de chamadas. Confira os números.")
        else:
            with engine.begin() as c:
                c.execute(text("DELETE FROM call_records WHERE event_date=:d AND team=:team AND operator=:operator AND source_file=:source"),
                          {"d": entry_date, "team": "Geral", "operator": "Geral", "source": manual_source})
                c.execute(text("INSERT INTO call_records(event_date,team,operator,total_calls,answered,abandoned,tme_seconds,source_file) VALUES (:d,:team,:operator,:calls,:answered,:abandoned,:tme,:source)"),
                          {"d": entry_date, "team": "Geral", "operator": "Geral", "calls": int(total_calls), "answered": int(answered), "abandoned": int(abandoned), "tme": float(tme_seconds), "source": manual_source})
            audit(user["username"], f"Lançamento manual geral de {entry_date}: {int(total_calls)} chamadas")
            st.success(f"Lançamento de {entry_date.strftime('%d/%m/%Y')} salvo com sucesso.")
            st.rerun()
    st.subheader("Lançamentos manuais anteriores")
    history = read_df("SELECT event_date AS Data, total_calls AS Chamadas, answered AS Atendidas, abandoned AS Abandonadas, tme_seconds AS TME FROM call_records WHERE source_file=:source ORDER BY event_date DESC", {"source": manual_source})
    if history.empty:
        st.info("Ainda não há lançamentos manuais.")
    else:
        history["Data"] = pd.to_datetime(history["Data"]).dt.strftime("%d/%m/%Y")
        history["TME"] = history["TME"].map(lambda x: f"{float(x):.2f} s")
        st.dataframe(history, use_container_width=True, hide_index=True)

if page == "Importação de dados":
    st.subheader("Importar relatórios CSV ou Excel")
    st.write("O arquivo é validado e armazenado no banco de dados. Para importação automática, coloque relatórios na pasta `imports/` e execute `python auto_import.py` via agendador do servidor.")
    st.code("Colunas aceitas: Data, Equipe, Operador, Chamadas, Atendidas, Abandonadas, TME (segundos)", language="text")
    files=st.file_uploader("Selecione um ou mais relatórios", type=["csv","xlsx","xls"], accept_multiple_files=True)
    if st.button("Importar arquivos", type="primary", disabled=not files):
        for f in files:
            try:
                n=import_dataframe(f.getvalue(), f.name, user["username"])
                st.success(f"{f.name}: {n} registros importados.")
            except Exception as e: st.error(f"Falha em {f.name}: {e}")
    st.subheader("Arquivos detectados na pasta de importação")
    folder_files=list(IMPORT_DIR.glob("*.csv"))+list(IMPORT_DIR.glob("*.xlsx"))+list(IMPORT_DIR.glob("*.xls"))
    st.write([f.name for f in folder_files] or "Nenhum arquivo encontrado.")
    if folder_files and st.button("Importar arquivos da pasta agora"):
        for f in folder_files:
            try:
                n=import_dataframe(f.read_bytes(), f.name, user["username"])
                st.success(f"{f.name}: {n} registros.")
            except Exception as e: st.error(f"{f.name}: {e}")

elif page == "Telefonia ao vivo (API)":
    st.subheader("Conexão com a API da telefonia")
    st.write("Esta página testa o endpoint de API informado nas variáveis de ambiente. O portal fornecido não expôs documentação pública acessível, então ainda precisamos descobrir o endpoint real e o formato da resposta antes de calcular indicadores com segurança.")
    api_url = os.getenv("VOIP_API_URL", "").strip()
    api_token = os.getenv("VOIP_API_TOKEN", "").strip()
    auth_header = os.getenv("VOIP_API_AUTH_HEADER", "Authorization").strip() or "Authorization"
    auth_prefix = os.getenv("VOIP_API_AUTH_PREFIX", "Bearer").strip()
    if not api_url:
        st.warning("A API ainda não está configurada. Não coloque um endereço inventado aqui: precisamos identificar a chamada real usada pelo portal.")
        st.markdown("**Como localizar o endpoint no navegador**")
        st.markdown("""1. Abra e entre no portal de telefonia em outra aba.
2. Pressione **F12** e abra a guia **Network / Rede**.
3. Selecione **Fetch/XHR** e atualize a página.
4. Clique nos pedidos que aparecem e procure respostas **JSON** com filas, chamadas, agentes ou estatísticas.
5. Copie apenas o **Request URL** e, se possível, um exemplo da resposta. Não compartilhe cookies, senhas, cabeçalhos de autorização nem tokens.""")
        st.info("Se não aparecer nenhum pedido Fetch/XHR, a página pode estar consultando diretamente o banco no servidor ou usando outro tipo de conexão. Envie uma captura da guia Network ou peça ao fornecedor a documentação/API de estatísticas de filas.")
    else:
        st.caption(f"Endpoint configurado: {api_url}")
        if st.button("Testar API agora", type="primary"):
            try:
                headers = {"Accept": "application/json"}
                if api_token:
                    headers[auth_header] = f"{auth_prefix} {api_token}".strip()
                req = urllib.request.Request(api_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=15) as response:
                    content_type = response.headers.get("Content-Type", "")
                    raw_body = response.read().decode("utf-8", errors="replace")
                    st.success(f"Resposta HTTP {response.status} · Content-Type: {content_type}")
                    try:
                        payload = json.loads(raw_body)
                        st.json(payload)
                        st.caption("Esta é a resposta bruta da API. Com ela identificaremos os campos exatos de TME, fila, atendidas, abandonadas e operadores antes de ligar os números aos cartões do dashboard.")
                    except json.JSONDecodeError:
                        st.warning("A resposta não é JSON. Pode ser HTML de login, CSV ou outro formato; não será interpretada como estatística automaticamente.")
                        st.text(raw_body[:12000])
            except urllib.error.HTTPError as e:
                st.error(f"A API respondeu HTTP {e.code}. Verifique endpoint, autenticação e permissões.")
            except Exception as e:
                st.error(f"Não foi possível consultar o endpoint: {e}")
    st.caption("Configuração no servidor: VOIP_API_URL (endpoint real), VOIP_API_TOKEN (se exigido), VOIP_API_AUTH_HEADER (opcional; padrão Authorization) e VOIP_API_AUTH_PREFIX (opcional; padrão Bearer). Mantenha segredos nas variáveis de ambiente, nunca no código ou em capturas de tela.")

elif page == "Metas e alertas":
    target=get_target()
    st.subheader("Meta de TME")
    if is_admin:
        with st.form("target_form"):
            new_target=st.number_input("Meta de TME (segundos)", min_value=0.0, max_value=3600.0, value=float(target), step=1.0)
            save=st.form_submit_button("Salvar meta")
        if save: set_target(new_target); audit(user["username"],f"Alterou meta de TME para {new_target}s"); st.success("Meta salva."); st.rerun()
    else: st.metric("Meta atual", f"{target:.1f} s")
    df=load_records()
    if df.empty: st.info("Importe dados para visualizar alertas.")
    else:
        by_day=df.groupby("Data",as_index=False).apply(lambda x: pd.Series({"TME": (x["TME"]*x["Atendidas"]).sum()/x["Atendidas"].sum() if x["Atendidas"].sum() else x["TME"].mean()}), include_groups=False).reset_index(drop=True)
        bad=by_day[by_day["TME"]>target]
        a,b=st.columns(2); a.metric("Dias acima da meta",len(bad)); b.metric("Maior TME",f"{df['TME'].max():.1f} s")
        if bad.empty: st.success("Nenhum dia acima da meta no conjunto de dados.")
        else: st.dataframe(bad.sort_values("TME",ascending=False),use_container_width=True,hide_index=True)
        st.caption("Esta versão exibe alertas dentro do painel. Envio de e-mail/SMS exige configurar um provedor e suas credenciais.")

elif page == "Usuários e acesso":
    if not is_admin:
        st.error("Apenas Administradores e Gestores podem acessar esta área.")
    else:
        st.subheader("Criar usuário")
        with st.form("new_user"):
            c1,c2=st.columns(2)
            new_name=c1.text_input("Nome completo")
            new_username=c2.text_input("Usuário de login")
            new_password=c1.text_input("Senha inicial",type="password")
            new_role=c2.selectbox("Nível de acesso",["Operador","Supervisor","Gestor","Administrador"])
            team=st.text_input("Equipe")
            add=st.form_submit_button("Criar usuário")
        if add:
            if not new_name or not new_username or len(new_password)<10: st.error("Preencha nome, usuário e senha com pelo menos 10 caracteres.")
            else:
                try:
                    with engine.begin() as c:
                        c.execute(text("INSERT INTO users(username,password_hash,full_name,role,team) VALUES (:u,:p,:n,:r,:t)"),
                                  {"u":new_username.strip(),"p":hash_password(new_password),"n":new_name.strip(),"r":new_role,"t":team.strip() or "Geral"})
                    audit(user["username"],f"Criou usuário {new_username}"); st.success("Usuário criado.")
                except Exception as e: st.error(f"Não foi possível criar usuário: {e}")
        st.subheader("Usuários cadastrados")
        users=read_df("SELECT username,full_name,role,team,active,created_at FROM users ORDER BY username")
        st.dataframe(users,use_container_width=True,hide_index=True)
        st.caption("A desativação e redefinição de senha podem ser feitas pelo administrador do banco nesta versão inicial. Evite compartilhar contas.")

elif page == "Configurações":
    st.subheader("Alterar sua senha")
    with st.form("change_password"):
        current=st.text_input("Senha atual",type="password")
        new=st.text_input("Nova senha (mínimo 10 caracteres)",type="password")
        confirm=st.text_input("Confirme a nova senha",type="password")
        change=st.form_submit_button("Alterar senha")
    if change:
        with engine.connect() as c:
            row=c.execute(text("SELECT password_hash FROM users WHERE username=:u"),{"u":user["username"]}).first()
        if not row or not verify_password(current,row[0]): st.error("Senha atual incorreta.")
        elif len(new)<10: st.error("A nova senha deve ter pelo menos 10 caracteres.")
        elif new!=confirm: st.error("As senhas não coincidem.")
        else:
            with engine.begin() as c: c.execute(text("UPDATE users SET password_hash=:p WHERE username=:u"),{"p":hash_password(new),"u":user["username"]})
            audit(user["username"],"Alterou a própria senha"); st.success("Senha alterada com sucesso.")
    st.subheader("Informações do sistema")
    st.write(f"Banco configurado: {'PostgreSQL' if DATABASE_URL.startswith('postgres') else 'SQLite (desenvolvimento)'}")
    st.write(f"Pasta de importação: `{IMPORT_DIR.resolve()}`")
    st.warning("Para produção na internet, use PostgreSQL, HTTPS, senha forte e variáveis de ambiente protegidas.")

else:
    today=date.today()
    with st.expander("Filtros",expanded=True):
        c1,c2,c3=st.columns(3)
        start=c1.date_input("Data inicial",today-timedelta(days=30))
        end=c2.date_input("Data final",today)
        raw_all=load_records()
        teams=sorted(raw_all["Equipe"].dropna().unique().tolist()) if not raw_all.empty else []
        operators=sorted(raw_all["Operador"].dropna().unique().tolist()) if not raw_all.empty else []
        selected_teams=c3.multiselect("Equipes",teams,default=teams)
        selected_ops=st.multiselect("Operadores",operators,default=operators)
    df=load_records(start,end)
    if not df.empty:
        df=df[df["Equipe"].isin(selected_teams) & df["Operador"].isin(selected_ops)]
    if page == "Dashboard":
        # Indicadores de TME por janela de tempo, calculados com todos os dados importados
        # (independentes do filtro de período do gráfico), respeitando equipes/operadores selecionados.
        all_records=load_records()
        if not all_records.empty:
            all_records=all_records[all_records["Equipe"].isin(selected_teams) & all_records["Operador"].isin(selected_ops)]

        def calc_tme(frame):
            if frame.empty:
                return None
            n_answered=float(frame["Atendidas"].sum())
            return float((frame["TME"]*frame["Atendidas"]).sum()/n_answered) if n_answered else float(frame["TME"].mean())

        week_start=today-timedelta(days=today.weekday())
        month_start=today.replace(day=1)
        year_start=today.replace(month=1,day=1)
        windows=[
            ("TME do dia", today, today),
            ("TME semanal", week_start, today),
            ("TME mensal", month_start, today),
            ("TME anual", year_start, today),
        ]
        st.subheader("Indicadores de TME por período")
        metric_cols=st.columns(4)
        for col,(label,period_start,period_end) in zip(metric_cols,windows):
            if all_records.empty:
                value=None
            else:
                period_df=all_records[(all_records["Data"].dt.date>=period_start) & (all_records["Data"].dt.date<=period_end)]
                value=calc_tme(period_df)
            col.metric(label, f"{value:.2f} s" if value is not None else "Sem dados")
        st.caption(f"Períodos: dia {today:%d/%m/%Y} · semana desde {week_start:%d/%m/%Y} · mês desde {month_start:%d/%m/%Y} · ano desde {year_start:%d/%m/%Y}. O TME é ponderado pelo número de atendidas.")

        if df.empty:
            st.info("Não há dados no período selecionado para os gráficos. Ajuste os filtros ou importe um relatório.")
        else:
            calls=int(df["Chamadas"].sum()); answered=int(df["Atendidas"].sum()); abandoned=int(df["Abandonadas"].sum())
            tme=float((df["TME"]*df["Atendidas"]).sum()/df["Atendidas"].sum()) if df["Atendidas"].sum() else float(df["TME"].mean())
            target=get_target()
            k1,k2,k3,k4=st.columns(4)
            k1.metric("Total de chamadas",f"{calls:,}".replace(",",".")); k2.metric("Atendidas",f"{answered:,}".replace(",","."))
            k3.metric("Abandonadas",f"{abandoned:,}".replace(",", "."),delta=f"{(abandoned/calls*100 if calls else 0):.1f}% do total",delta_color="inverse")
            k4.metric("TME médio",f"{tme:.1f} s",delta=f"Meta {target:.0f} s",delta_color="inverse" if tme>target else "normal")
            if tme>target: st.error(f"Alerta: TME está {tme-target:.1f} segundos acima da meta.")
            else: st.success("TME dentro da meta.")
            daily=df.groupby("Data",as_index=False).apply(lambda x: pd.Series({"TME":(x["TME"]*x["Atendidas"]).sum()/x["Atendidas"].sum() if x["Atendidas"].sum() else x["TME"].mean(),"Atendidas":x["Atendidas"].sum(),"Abandonadas":x["Abandonadas"].sum()}),include_groups=False).reset_index(drop=True)
            left,right=st.columns(2)
            with left:
                fig=px.line(daily,x="Data",y="TME",markers=True,title="Evolução diária do TME (segundos)")
                fig.add_hline(y=target,line_dash="dash",annotation_text=f"Meta {target:.0f}s")
                st.plotly_chart(fig,use_container_width=True)
            with right:
                comp=df.groupby("Equipe",as_index=False).apply(lambda x: pd.Series({"TME":(x["TME"]*x["Atendidas"]).sum()/x["Atendidas"].sum() if x["Atendidas"].sum() else x["TME"].mean(),"Atendidas":x["Atendidas"].sum()}),include_groups=False).reset_index(drop=True)
                st.plotly_chart(px.bar(comp,x="Equipe",y="TME",color="Equipe",title="TME por equipe"),use_container_width=True)
            l2,r2=st.columns(2)
            with l2:
                dist=pd.DataFrame({"Status":["Atendidas","Abandonadas"],"Chamadas":[answered,abandoned]})
                st.plotly_chart(px.pie(dist,names="Status",values="Chamadas",hole=.55,title="Distribuição de chamadas"),use_container_width=True)
            with r2:
                op=df.groupby(["Equipe","Operador"],as_index=False).apply(lambda x: pd.Series({"TME":(x["TME"]*x["Atendidas"]).sum()/x["Atendidas"].sum() if x["Atendidas"].sum() else x["TME"].mean(),"Atendidas":x["Atendidas"].sum()}),include_groups=False).reset_index(drop=True)
                st.plotly_chart(px.bar(op.sort_values("TME"),x="Operador",y="TME",color="Equipe",title="Comparação entre operadores"),use_container_width=True)
            st.subheader("Detalhamento diário")
            st.dataframe(daily.sort_values("Data",ascending=False),use_container_width=True,hide_index=True)
    elif page == "TME Diário":
        st.write("Escolha **Dashboard** para gráficos e detalhamento diário; exportações ficam em **Relatórios**.")
    elif page == "TME Semanal":
        if df.empty: st.info("Sem dados no período.")
        else:
            weekly=df.groupby("Semana",as_index=False).apply(lambda x: pd.Series({"Atendidas":x["Atendidas"].sum(),"Abandonadas":x["Abandonadas"].sum(),"TME":(x["TME"]*x["Atendidas"]).sum()/x["Atendidas"].sum() if x["Atendidas"].sum() else x["TME"].mean()}),include_groups=False).reset_index(drop=True)
            st.plotly_chart(px.line(weekly,x="Semana",y="TME",markers=True,title="TME semanal"),use_container_width=True)
            st.dataframe(weekly,use_container_width=True,hide_index=True)
    elif page == "Relatórios":
        if df.empty: st.info("Sem dados para gerar relatórios.")
        else:
            st.subheader("Exportar dados filtrados")
            xbuf=io.BytesIO()
            with pd.ExcelWriter(xbuf,engine="openpyxl") as writer:
                df.to_excel(writer,index=False,sheet_name="Detalhamento")
                df.groupby("Semana",as_index=False).agg(Atendidas=("Atendidas","sum"),Abandonadas=("Abandonadas","sum"),TME=("TME","mean")).to_excel(writer,index=False,sheet_name="Semanal")
                df.groupby("Equipe",as_index=False).agg(Chamadas=("Chamadas","sum"),Atendidas=("Atendidas","sum"),Abandonadas=("Abandonadas","sum"),TME=("TME","mean")).to_excel(writer,index=False,sheet_name="Equipes")
            st.download_button("⬇️ Baixar Excel",xbuf.getvalue(),file_name="relatorio_tme.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            try:
                from reportlab.lib import colors
                from reportlab.lib.pagesizes import landscape, A4
                from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
                from reportlab.lib.styles import getSampleStyleSheet
                pbuf=io.BytesIO(); doc=SimpleDocTemplate(pbuf,pagesize=landscape(A4))
                styles=getSampleStyleSheet(); story=[Paragraph("Relatório de TME e Telefonia",styles["Title"]),Paragraph(f"Período: {start} a {end}",styles["Normal"]),Spacer(1,12)]
                summary=[["Indicador","Valor"],["Chamadas",str(int(df["Chamadas"].sum()))],["Atendidas",str(int(df["Atendidas"].sum()))],["Abandonadas",str(int(df["Abandonadas"].sum()))],["TME médio (s)",f"{(df['TME']*df['Atendidas']).sum()/df['Atendidas'].sum() if df['Atendidas'].sum() else df['TME'].mean():.2f}"]]
                table=Table(summary,repeatRows=1); table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#174a7e")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.5,colors.grey),("PADDING",(0,0),(-1,-1),7)]))
                story.extend([table,Spacer(1,16),Paragraph("Resumo por equipe",styles["Heading2"])])
                team=df.groupby("Equipe",as_index=False).agg(Chamadas=("Chamadas","sum"),Atendidas=("Atendidas","sum"),Abandonadas=("Abandonadas","sum"),TME=("TME","mean"))
                data=[list(team.columns)]+[[str(v) for v in row] for row in team.round(2).values.tolist()]
                table2=Table(data,repeatRows=1); table2.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#174a7e")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.5,colors.grey),("PADDING",(0,0),(-1,-1),6)]))
                story.append(table2); doc.build(story)
                st.download_button("⬇️ Baixar PDF",pbuf.getvalue(),file_name="relatorio_tme.pdf",mime="application/pdf")
            except ImportError: st.warning("Instale reportlab para exportar PDF.")
    elif page == "Equipes e operadores":
        if df.empty: st.info("Sem dados para comparar.")
        else:
            summary=df.groupby(["Equipe","Operador"],as_index=False).agg(Chamadas=("Chamadas","sum"),Atendidas=("Atendidas","sum"),Abandonadas=("Abandonadas","sum"),TME=("TME","mean"))
            summary["Taxa de abandono (%)"]=(summary["Abandonadas"]/summary["Chamadas"].replace(0, pd.NA)*100).round(2)
            st.dataframe(summary.sort_values("TME"),use_container_width=True,hide_index=True)
            st.plotly_chart(px.bar(summary.sort_values("TME"),x="Operador",y="TME",color="Equipe",title="TME por operador"),use_container_width=True)

st.divider()
st.caption("TME Analytics · Controle de acesso por perfil · Confirme as regras de negócio e o significado dos códigos do seu relatório antes de usar os indicadores para decisões operacionais.")
