/* Format-aware default-profile parser loops. The public hint selects one loop
 * once per stream; no format branch exists inside a match search. */
#include "defl.h"
#include "profiles.h"

/* Keep the format policies independently overridable for cache/instruction
 * sweeps.  Production builds use the defaults below; an experiment can change
 * one loop with e.g. PARSEOPT_OVERRIDE=-DGZPF_JSON_CHAIN=32 without perturbing
 * the generic parser or the other format-specialised loops. */
#ifndef GZPF_JSON_CHAIN
#define GZPF_JSON_CHAIN 16
#endif
#ifndef GZPF_GRAPHQL_CHAIN
#define GZPF_GRAPHQL_CHAIN 8
#endif
#ifndef GZPF_TEXT_CHAIN
#define GZPF_TEXT_CHAIN 16
#endif
#ifndef GZPF_PLAINTEXT_CHAIN
#define GZPF_PLAINTEXT_CHAIN 16
#endif
#ifndef GZPF_LOG_CHAIN
#define GZPF_LOG_CHAIN 12
#endif

/* JSON: per-byte short-match pricing wins a little ratio at lower cost than
 * the must-beat-both generic rule. */
#define GZPF_NAME json
#define GZPF_CHAIN GZPF_JSON_CHAIN
#define GZPF_SHORTPRICE 1
#define GZPF_SHORTMODE 2
#include "parse_format_template.h"

/* GraphQL: long, dense repetition needs fewer probes and no short-match cost
 * model at all. */
#define GZPF_NAME graphql
#define GZPF_CHAIN GZPF_GRAPHQL_CHAIN
#define GZPF_SHORTPRICE 0
#define GZPF_SHORTMODE 0
#include "parse_format_template.h"

/* HTML: moderate search, take four-byte matches directly. */
#define GZPF_NAME text
#define GZPF_CHAIN GZPF_TEXT_CHAIN
#define GZPF_SHORTPRICE 0
#define GZPF_SHORTMODE 0
#include "parse_format_template.h"

/* Mixed HTML can spend a deep chain finding only short matches.  The stream
 * triage counters select this one-probe tail when that happens; highly
 * redundant HTML keeps the deeper text parser above. */
#define GZPF_NAME textshallow
#define GZPF_CHAIN 1
#define GZPF_SHORTPRICE 0
#define GZPF_SHORTMODE 0
#include "parse_format_template.h"

/* Plaintext is the generic encoder's narrowest lead over libdeflate, so spend
 * fewer probes: the ratio remains within one percent while the instruction
 * margin becomes comfortably double-digit. */
#define GZPF_NAME plaintext
#define GZPF_CHAIN GZPF_PLAINTEXT_CHAIN
#define GZPF_SHORTPRICE 0
#define GZPF_SHORTMODE 0
#include "parse_format_template.h"

/* Logs need the generic must-beat-both short-match filter, but not all 48
 * probes. */
#define GZPF_NAME log
#define GZPF_CHAIN GZPF_LOG_CHAIN
#define GZPF_SHORTPRICE 1
#define GZPF_SHORTMODE 3
#include "parse_format_template.h"
