-- Sugerencias de validación por IA sobre candidatos de glosario. Son solo propuestas:
-- nada pasa a translation_glossary hasta que una persona las aplique.
ALTER TABLE glossary_candidates
 ADD COLUMN IF NOT EXISTS ai_verdict text CHECK (ai_verdict IN ('approve','reject','review')),
 ADD COLUMN IF NOT EXISTS ai_term text,
 ADD COLUMN IF NOT EXISTS ai_reason text,
 ADD COLUMN IF NOT EXISTS ai_checked_at timestamptz;
