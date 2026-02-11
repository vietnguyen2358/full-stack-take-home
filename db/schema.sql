-- Clones table: stores each clone request and its result
create table if not exists clones (
  id uuid primary key default gen_random_uuid(),
  url text not null,
  generated_code text,
  preview_url text,
  screenshot_count int default 0,
  image_count int default 0,
  html_raw_size int default 0,
  html_cleaned_size int default 0,
  status text not null default 'pending', -- pending, scraping, generating, deploying, done, error
  error_message text,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

-- Index for listing recent clones
create index if not exists idx_clones_created_at on clones (created_at desc);
