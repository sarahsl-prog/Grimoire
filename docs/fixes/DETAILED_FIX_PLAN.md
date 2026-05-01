# Detailed Fix Plan for Grimoire Issues

Based on the comprehensive analysis, here's a prioritized plan to address all outstanding issues:

## Phase 1: Critical Logic Errors (Highest Priority)

### 1.1 Fix Parser Double Hash Computation
**Module:** `grimoire/core/parser.py`  
**Issue:** Lines 444 and 513 both compute file hashes  
**Tasks:**
- Remove redundant hash computation in `_parse_sync()` (line 513)
- Ensure only one hash computation occurs per file parse
- Add test to verify hash is computed exactly once

### 1.2 Replace Deprecated Asyncio Call
**Module:** `grimoire/core/parser.py`  
**Issue:** Line 453 uses deprecated `asyncio.get_event_loop()`  
**Tasks:**
- Replace with `asyncio.get_running_loop()`
- Verify async context compatibility
- Test with async parsing scenarios

### 1.3 Fix Exception Handler Conflicts
**Module:** `grimoire/core/parser.py`  
**Issue:** Conflicting success/failed status handlers  
**Tasks:**
- Consolidate exception handling logic
- Ensure consistent error reporting
- Test error scenarios thoroughly

### 1.4 Remove Mock Detection in Production Code
**Module:** `grimoire/core/parser.py`  
**Issue:** Lines 247,255 contain test scaffolding  
**Tasks:**
- Remove `_mock_name` attribute checks
- Ensure production code doesn't reference test artifacts
- Verify functionality remains intact

## Phase 2: Test Infrastructure Fixes

### 2.1 Fix Database Model Test Failures
**Module:** `tests/test_db_models.py`  
**Issue:** 55 import errors blocking CI  
**Tasks:**
- Investigate missing test fixtures/conftest support
- Ensure test database setup works correctly
- Fix all ModuleNotFoundError issues
- Verify all 55 errors resolved

### 2.2 Fix Tagger Test Failures
**Module:** `tests/test_tagger.py`  
**Issue:** 6 import/module errors  
**Tasks:**
- Resolve module import issues
- Ensure test dependencies are properly configured
- Verify all tagger tests pass

## Phase 3: Missing Component Implementations

### 3.1 Create Shared Agent Utilities
**Module:** `grimoire/agents/base.py`  
**Tasks:**
- Implement shared agent utilities as documented in DESIGN.md
- Include common error handling patterns
- Add logging setup utilities

### 3.2 Implement Vector Search Wrapper
**Module:** `grimoire/search/vector.py`  
**Tasks:**
- Create vector search abstraction layer
- Implement wrapper around vectorstore operations
- Add proper error handling

### 3.3 Add Migration CLI Command
**Module:** `grimoire/cli/main.py`  
**Tasks:**
- Implement `grimoire migrate` command
- Add `--to qdrant` option as documented
- Connect to migration functionality

### 3.4 Implement Cache Stats CLI
**Module:** `grimoire/cli/status.py`  
**Tasks:**
- Replace bare `except: pass` with proper implementation
- Add cache statistics display functionality
- Implement `grimoire cache stats` command

### 3.5 Add Missing Utility Modules
**Modules:** `grimoire/utils/{hash,path,rate_limit,observability}.py`  
**Tasks:**
- Create hash utility functions
- Implement path normalization utilities
- Add rate limiting functionality
- Create observability/logging helpers

## Phase 4: Documentation Alignment

### 4.1 Update DESIGN.md
**Tasks:**
- Remove references to non-existent files
- Add missing wiki pages documentation
- Align project structure with actual implementation

### 4.2 Align CLI Documentation
**Tasks:**
- Update README.md to match actual CLI commands
- Correct `grimoire tag` command documentation
- Verify all CLI examples work

## Phase 5: Test Coverage Improvement

### 5.1 Improve Reranker Coverage
**Module:** `grimoire/core/reranker.py` (32% → 85%+)  
**Tasks:**
- Add unit tests for all reranker methods
- Include edge cases and error scenarios
- Test cross-encoder implementation

### 5.2 Improve Session Coverage
**Module:** `grimoire/db/session.py` (28% → 85%+)  
**Tasks:**
- Add tests for session management functions
- Test error handling scenarios
- Include async session behavior tests

### 5.3 Improve Wiki CLI Coverage
**Module:** `grimoire/cli/wiki.py` (17% → 85%+)  
**Tasks:**
- Add comprehensive CLI command tests
- Test argument parsing
- Include integration tests with wiki agent

## Phase 6: Lint and Type Error Resolution

### 6.1 Fix Ruff Lint Errors (154 total)
**Priority Fixes:**
- Fix E402 import placement in `cli/main.py`
- Remove F401 unused imports
- Fix S110 bare except statements
- Address S603 subprocess security issues
- Correct F541 f-string formatting errors

### 6.2 Fix MyPy Type Errors (86 total)
**Priority Fixes:**
- Fix datetime import scoping in `db/base.py`
- Add type annotations to `get_db_context`
- Correct SQLEnum typing in `db/models.py`
- Fix subclassing Any issues in storage modules

## Implementation Timeline

### Week 1:
1. Fix critical parser logic errors (1.1-1.4)
2. Resolve test infrastructure failures (2.1-2.2)
3. Begin implementing missing components (3.1-3.2)

### Week 2:
1. Complete missing component implementations (3.3-3.5)
2. Start test coverage improvements (5.1-5.3)
3. Begin lint/type error resolution (6.1-6.2)

### Week 3:
1. Complete documentation alignment (4.1-4.2)
2. Finish test coverage improvements
3. Finalize lint/type error resolution

### Week 4:
1. Final integration testing
2. Coverage verification (85%+ target)
3. CI/CD pipeline validation

## Success Metrics

1. **Test Results:** 0 failures, 0 errors
2. **Coverage:** ≥85% for all core modules
3. **Lint:** 0 ruff errors
4. **Type Checking:** 0 mypy errors
5. **Documentation:** 100% alignment between code and docs
6. **CI:** Clean execution with all tests passing