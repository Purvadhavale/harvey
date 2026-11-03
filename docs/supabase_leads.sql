-- Tables for `harvey sync` (and `harvey export --supabase`). Run once in the
-- Supabase SQL editor of the project SUPABASE_URL points at.
-- brands: one row per company. contacts: people and inboxes at a brand.

create table if not exists public.brands (
  id              text primary key,             -- Harvey's company id
  name            text not null default '',
  domain          text not null unique,
  website         text not null default '',
  sector          text not null default '',
  city            text not null default '',
  is_junk         boolean not null default false,  -- on the do-not-contact list
  fit             integer not null default 0,      -- 0-100, see harvey/fit.py
  fit_reasons     text not null default '',
  next_step       text not null default '',
  phone           text not null default '',        -- validated only
  phone_type      text not null default '',        -- mobile | landline | toll_free | ...
  phone_source    text not null default '',        -- website | listing
  linkedin_page   text not null default '',
  instagram       text not null default '',
  marketing_email text not null default '',
  meta_ads        boolean,
  google_ads      boolean,
  tech_stack      text not null default '',
  hiring_roles    text not null default '',
  funding_round   text not null default '',
  first_found_at  timestamptz,
  synced_at       timestamptz not null default now()
);

create table if not exists public.contacts (
  id            text primary key,                  -- Harvey's prospect id
  brand_id      text not null references public.brands(id) on delete cascade,
  first_name    text not null default '',
  last_name     text not null default '',
  title         text not null default '',
  email         text not null default '',
  email_status  text not null default '',          -- verified | risky | guess | invalid | ''
  linkedin_url  text not null default '',
  source        text not null default '',          -- linkedin_search | linkedin_team | known_company_backfill | company_inbox
  is_inbox      boolean not null default false,
  created_at    timestamptz,
  synced_at     timestamptz not null default now()
);

create index if not exists contacts_brand_id_idx on public.contacts(brand_id);
create index if not exists brands_fit_idx on public.brands(fit desc);

-- Who may read the lead list from a signed-in app (e.g. a hosted dashboard).
-- The sync itself uses the secret key, which bypasses these policies.
create table if not exists public.lead_viewers (
  email      text primary key,
  added_at   timestamptz not null default now()
);

alter table public.brands       enable row level security;
alter table public.contacts     enable row level security;
alter table public.lead_viewers enable row level security;

create policy "listed viewers read brands" on public.brands
  for select to authenticated
  using (exists (select 1 from public.lead_viewers v where v.email = (auth.jwt() ->> 'email')));

create policy "listed viewers read contacts" on public.contacts
  for select to authenticated
  using (exists (select 1 from public.lead_viewers v where v.email = (auth.jwt() ->> 'email')));

create policy "viewers see their own row" on public.lead_viewers
  for select to authenticated
  using (email = (auth.jwt() ->> 'email'));
