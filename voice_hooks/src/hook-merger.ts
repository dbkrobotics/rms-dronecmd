interface Hook {
  type: string;
  command: string;
}

interface HookConfig {
  matcher: string;
  hooks: Hook[];
}

export interface HookSettings {
  [hookType: string]: HookConfig[];
}

/**
 * Removes any existing voice hooks that use MCP_VOICE_HOOKS_PORT
 * @param hooks - The current hooks configuration
 * @returns The hooks with voice hooks removed
 */
export function removeVoiceHooks(hooks: HookSettings = {}): HookSettings {
  const cleaned: HookSettings = {};
  // Pattern to match voice hooks by the unique environment variable
  const voiceHookPattern = /MCP_VOICE_HOOKS_PORT/;
  
  for (const [hookType, hookArray] of Object.entries(hooks)) {
    cleaned[hookType] = hookArray.filter(hookConfig => {
      // Keep this hook config only if none of its commands match our pattern
      return !hookConfig.hooks.some(hook => voiceHookPattern.test(hook.command));
    });
    
    // Remove empty arrays
    if (cleaned[hookType].length === 0) {
      delete cleaned[hookType];
    }
  }
  
  return cleaned;
}

/**
 * Replaces voice hooks - removes any existing ones and adds new ones
 * @param existingHooks - The current hooks configuration
 * @param voiceHooks - The voice hooks to add
 * @returns The updated hooks configuration
 */
export function replaceVoiceHooks(existingHooks: HookSettings = {}, voiceHooks: HookSettings): HookSettings {
  // First, remove any existing voice hooks
  const cleaned = removeVoiceHooks(existingHooks);
  
  // Then merge in the new voice hooks
  const result: HookSettings = JSON.parse(JSON.stringify(cleaned)); // Deep clone
  
  for (const [hookType, hookArray] of Object.entries(voiceHooks)) {
    if (!result[hookType]) {
      result[hookType] = hookArray;
    } else {
      result[hookType].push(...hookArray);
    }
  }
  
  return result;
}

/**
 * Checks if two hook settings are semantically equal (ignoring order)
 * @param hooks1 - First hooks configuration
 * @param hooks2 - Second hooks configuration
 * @returns True if they contain the same hooks regardless of order
 */
export function areHooksEqual(hooks1: HookSettings = {}, hooks2: HookSettings = {}): boolean {
  const types1 = Object.keys(hooks1).sort();
  const types2 = Object.keys(hooks2).sort();
  
  // Different hook types
  if (types1.join(',') !== types2.join(',')) {
    return false;
  }
  
  // Check each hook type
  for (const hookType of types1) {
    const configs1 = hooks1[hookType];
    const configs2 = hooks2[hookType];
    
    if (configs1.length !== configs2.length) {
      return false;
    }
    
    // Create normalized strings for comparison
    const normalized1 = configs1
      .map(config => JSON.stringify(config))
      .sort();
    const normalized2 = configs2
      .map(config => JSON.stringify(config))
      .sort();
    
    // Compare sorted arrays
    for (let i = 0; i < normalized1.length; i++) {
      if (normalized1[i] !== normalized2[i]) {
        return false;
      }
    }
  }
  
  return true;
}