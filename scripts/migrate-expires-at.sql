-- Migration: Add expires_at column for TTL support
ALTER TABLE memories ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
