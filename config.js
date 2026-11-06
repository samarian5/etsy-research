// Public by design: the anon key only ever sees what RLS allows (SELECT),
// and every write goes through a token-gated RPC. Safe to commit.
window.ETSY_CONFIG = {
  "supabaseUrl": "https://bilwnxynjjeinktcpmpd.supabase.co",
  "anonKey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJpbHdueHluamplaW5rdGNwbXBkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM3OTczMjQsImV4cCI6MjEwOTM3MzMyNH0.xlw8gB3_tACg45aKt4AvaYJtRidE9mgjHoWuL3F1NQ8"
};
