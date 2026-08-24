// Copyright 2018 Ulf Adams
//
// The contents of this file may be used under the terms of the Apache License,
// Version 2.0.
//
//    (See accompanying file LICENSE-Apache or copy at
//     http://www.apache.org/licenses/LICENSE-2.0)
//
// Alternatively, the contents of this file may be used under the terms of
// the Boost Software License, Version 1.0.
//    (See accompanying file LICENSE-Boost or copy at
//     https://www.boost.org/LICENSE_1_0.txt)
//
// Unless required by applicable law or agreed to in writing, this software
// is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.
#ifndef WREATH_RYU_H
#define WREATH_RYU_H

#ifdef __cplusplus
extern "C" {
#endif

/* Write the exact spelling produced by Python's repr-mode float formatter,
 * including its fixed/scientific threshold and exponent punctuation. The
 * caller supplies 25 bytes. Returns the length, or -1 for a non-finite value. */
int wreath_ryu_d2s(double value, char *result);

#ifdef __cplusplus
}
#endif

#endif /* WREATH_RYU_H */
