-- ==========================================================
-- Финансовый дом — схема БД (MVP)
-- Выполнить в Supabase SQL editor
-- ==========================================================
--
-- МИГРАЦИЯ (если таблицы уже созданы по старой схеме — без Google-колонок
-- и без owner_user_id): выполни это ПЕРЕД остальным файлом, дальше все
-- "create table if not exists" ниже безопасно пропустятся для уже
-- существующих таблиц.
--
-- alter table users
--   add column if not exists google_email text,
--   add column if not exists google_access_token text,
--   add column if not exists google_refresh_token text,
--   add column if not exists google_spreadsheet_id text;
--
-- alter table family
--   add column if not exists owner_user_id bigint references users(id);

create table if not exists users (
    id bigserial primary key,
    tg_id bigint unique not null,
    username text,
    currency text default 'RUB',           -- RUB / USD / EUR
    month_start int default 1,             -- день начала отчётного периода (1-28)
    cash_on_hand numeric,                   -- опционально, NULL если пропустил
    pin_hash text,                          -- NULL если PIN не установлен
    onboarding_done boolean default false,
    -- Google Sheets (drive.file scope) — каждый подключает СВОЙ Диск при
    -- онбординге, независимо от участия в семье (см. owner_user_id ниже).
    google_email text,
    google_access_token text,
    google_refresh_token text,
    google_spreadsheet_id text,             -- ID личной таблицы этого пользователя
    created_at timestamptz default now()
);

create table if not exists family (
    id bigserial primary key,
    owner_user_id bigint references users(id),  -- инициатор /family — его Sheets общие
    created_at timestamptz default now()
);

create table if not exists family_members (
    family_id bigint references family(id) on delete cascade,
    user_id bigint references users(id) on delete cascade,
    joined_at timestamptz default now(),
    primary key (family_id, user_id)
);

create table if not exists family_invites (
    id bigserial primary key,
    from_user_id bigint references users(id) on delete cascade,
    to_tg_id bigint not null,
    status text default 'pending',          -- pending / accepted / declined
    created_at timestamptz default now()
);

create table if not exists categories (
    id bigserial primary key,
    user_id bigint references users(id) on delete cascade,
    name text not null,
    is_custom boolean default false,
    created_at timestamptz default now(),
    unique (user_id, name)
);

create table if not exists category_map (
    user_id bigint references users(id) on delete cascade,
    keyword text not null,
    category text not null,
    created_at timestamptz default now(),
    primary key (user_id, keyword)
);

-- ⚠️ УСТАРЕЛО: сами транзакции переехали в Google Sheets владельца (лист
-- "Транзакции" — см. sheets_transactions.py). Текущий код (input_handler.py,
-- report.py, history.py, undo.py) больше вообще не читает и не пишет в эту
-- таблицу. Создавать её НЕ обязательно для новой установки — оставлена
-- здесь только для тех, у кого уже есть старые данные в ней и кто хочет
-- решить, переносить ли их вручную, перед тем как выполнить DROP TABLE.
create table if not exists transactions (
    id bigserial primary key,
    user_id bigint references users(id) on delete cascade,
    family_id bigint references family(id),          -- NULL если не в семье
    amount numeric not null,
    type text not null,                                -- income / expense
    category text not null,
    date_time timestamptz default now(),
    source text default 'text',                        -- text / voice / receipt
    comment text,
    is_deleted boolean default false                   -- для /undo (soft delete)
);

create index if not exists idx_transactions_user on transactions(user_id, date_time desc);
create index if not exists idx_transactions_family on transactions(family_id, date_time desc);

-- Дефолтные категории проставляются в коде при онбординге (INSERT ... on conflict do nothing)
