-- Migration: Add independent temperature control for TTS models
-- Adds tts_temperature to chats table
ALTER TABLE chats ADD COLUMN IF NOT EXISTS tts_temperature FLOAT;
