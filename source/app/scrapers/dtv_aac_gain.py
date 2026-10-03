"""DirecTV DAI: attenuate inserted AAC ads a fixed amount, losslessly.

The inserted-ad AAC twins (see dtv_aac_ads) play a few dB hotter than the
stereo-downmixed AC-3 programming, so breaks blast. This drops every inserted
ad's loudness by editing the AAC-LC ``global_gain`` field of every channel of
every frame -- the same lossless lever mp3gain/aacgain use -- implemented here
from the ISO/IEC 14496-3 bitstream syntax. 3 steps x 1.5 dB = -4.5 dB; no
re-encoding, no dependencies.

A stereo ad is a channel_pair_element whose second channel's ``global_gain``
sits *after* the first channel's Huffman-coded spectral data, so each frame is
parsed in full to locate it. Safety is absolute: a frame is edited only if the
whole raw_data_block parses cleanly to END and byte-aligns at the frame
boundary under a detected sample rate; on any mismatch or error the original
bytes are returned unchanged, so a mis-parse can never emit a corrupt segment.
"""
from __future__ import annotations

import re

# AAC Huffman + scalefactor-band tables, ISO/IEC 14496-3. Generated; do not hand-edit.
_SCF_LENS = [
    18, 18, 18, 18, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 18, 19, 18,
    17, 17, 16, 17, 16, 16, 16, 16, 15, 15, 14, 14, 14, 14, 14, 14, 13, 13, 12, 12, 12, 11,
    12, 11, 10, 10, 10, 9, 9, 8, 8, 8, 7, 6, 6, 5, 4, 3, 1, 4, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9,
    10, 10, 10, 11, 11, 11, 11, 12, 12, 13, 13, 13, 14, 14, 16, 15, 16, 15, 18, 19, 19, 19,
    19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19,
    19, 19, 19, 19, 19
]
_SCF_CODES = [
    262120, 262118, 262119, 262117, 524277, 524273, 524269, 524278, 524270, 524271, 524272,
    524284, 524285, 524287, 524286, 524279, 524280, 524283, 524281, 262116, 524282, 262115,
    131055, 131056, 65525, 131054, 65522, 65523, 65524, 65521, 32758, 32759, 16377, 16373,
    16375, 16371, 16374, 16370, 8183, 8181, 4089, 4087, 4086, 2041, 4084, 2040, 1017, 1015,
    1013, 504, 503, 250, 248, 246, 121, 58, 56, 26, 11, 4, 0, 10, 12, 27, 57, 59, 120, 122,
    247, 249, 502, 505, 1012, 1014, 1016, 2037, 2036, 2038, 2039, 4085, 4088, 8180, 8182,
    8184, 16376, 16372, 65520, 32756, 65526, 32757, 262114, 524249, 524250, 524251, 524252,
    524253, 524254, 524248, 524242, 524243, 524244, 524245, 524246, 524274, 524255, 524263,
    524264, 524265, 524266, 524267, 524262, 524256, 524257, 524258, 524259, 524260, 524261,
    524247, 524268, 524276, 524275
]
_SPEC_LENS = [
    [11, 9, 11, 10, 7, 10, 11, 9, 11, 10, 7, 10, 7, 5, 7, 9, 7, 10, 11, 9, 11, 9, 7, 9, 11, 9, 11, 9, 7, 9, 7, 5, 7, 9, 7, 9, 7, 5, 7, 5, 1, 5, 7, 5, 7, 9, 7, 9, 7, 5, 7, 9, 7, 9, 11, 9, 11, 9, 7, 9, 11, 9, 11, 10, 7, 9, 7, 5, 7, 9, 7, 10, 11, 9, 11, 10, 7, 9, 11, 9, 11],
    [9, 7, 9, 8, 6, 8, 9, 8, 9, 8, 6, 7, 6, 5, 6, 7, 6, 8, 9, 7, 8, 8, 6, 8, 9, 7, 9, 8, 6, 7, 6, 5, 6, 7, 6, 8, 6, 5, 6, 5, 3, 5, 6, 5, 6, 8, 6, 7, 6, 5, 6, 8, 6, 8, 9, 7, 9, 8, 6, 8, 8, 7, 9, 8, 6, 7, 6, 4, 6, 8, 6, 7, 9, 7, 9, 7, 6, 8, 9, 7, 9],
    [1, 4, 8, 4, 5, 8, 9, 9, 10, 4, 6, 9, 6, 6, 9, 9, 9, 10, 9, 10, 13, 9, 9, 11, 11, 10, 12, 4, 6, 10, 6, 7, 10, 10, 10, 12, 5, 7, 11, 6, 7, 10, 9, 9, 11, 9, 10, 13, 8, 9, 12, 10, 11, 12, 8, 10, 15, 9, 11, 15, 13, 14, 16, 8, 10, 14, 9, 10, 14, 12, 12, 15, 11, 12, 16, 10, 11, 15, 12, 12, 15],
    [4, 5, 8, 5, 4, 8, 9, 8, 11, 5, 5, 8, 5, 4, 8, 8, 7, 10, 9, 8, 11, 8, 8, 10, 11, 10, 11, 4, 5, 8, 4, 4, 8, 8, 8, 10, 4, 4, 8, 4, 4, 7, 8, 7, 9, 8, 8, 10, 7, 7, 9, 10, 9, 10, 8, 8, 11, 8, 7, 10, 11, 10, 12, 8, 7, 10, 7, 7, 9, 10, 9, 11, 11, 10, 12, 10, 9, 11, 11, 10, 11],
    [13, 12, 11, 11, 10, 11, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10, 11, 12, 12, 10, 9, 8, 7, 8, 9, 10, 11, 11, 9, 8, 5, 4, 5, 8, 9, 11, 10, 8, 7, 4, 1, 4, 7, 8, 11, 11, 9, 8, 5, 4, 5, 8, 9, 11, 11, 10, 9, 8, 7, 8, 9, 10, 11, 12, 11, 10, 9, 8, 9, 10, 11, 12, 13, 12, 12, 11, 10, 10, 11, 12, 13],
    [11, 10, 9, 9, 9, 9, 9, 10, 11, 10, 9, 8, 7, 7, 7, 8, 9, 10, 9, 8, 6, 6, 6, 6, 6, 8, 9, 9, 7, 6, 4, 4, 4, 6, 7, 9, 9, 7, 6, 4, 4, 4, 6, 7, 9, 9, 7, 6, 4, 4, 4, 6, 7, 9, 9, 8, 6, 6, 6, 6, 6, 8, 9, 10, 9, 8, 7, 7, 7, 7, 8, 10, 11, 10, 9, 9, 9, 9, 9, 10, 11],
    [1, 3, 6, 7, 8, 9, 10, 11, 3, 4, 6, 7, 8, 8, 9, 9, 6, 6, 7, 8, 8, 9, 9, 10, 7, 7, 8, 8, 9, 9, 10, 10, 8, 8, 9, 9, 10, 10, 10, 11, 9, 8, 9, 9, 10, 10, 11, 11, 10, 9, 9, 10, 10, 11, 12, 12, 11, 10, 10, 10, 11, 11, 12, 12],
    [5, 4, 5, 6, 7, 8, 9, 10, 4, 3, 4, 5, 6, 7, 7, 8, 5, 4, 4, 5, 6, 7, 7, 8, 6, 5, 5, 6, 6, 7, 8, 8, 7, 6, 6, 6, 7, 7, 8, 9, 8, 7, 6, 7, 7, 8, 8, 10, 9, 7, 7, 8, 8, 8, 9, 9, 10, 8, 8, 8, 9, 9, 9, 10],
    [1, 3, 6, 8, 9, 10, 10, 11, 11, 12, 12, 13, 13, 3, 4, 6, 7, 8, 8, 9, 10, 10, 10, 11, 12, 12, 6, 6, 7, 8, 8, 9, 10, 10, 10, 11, 12, 12, 12, 8, 7, 8, 9, 9, 10, 10, 11, 11, 11, 12, 12, 13, 9, 8, 9, 9, 10, 10, 11, 11, 11, 12, 12, 12, 13, 10, 9, 9, 10, 11, 11, 11, 12, 11, 12, 12, 13, 13, 11, 9, 10, 11, 11, 11, 12, 12, 12, 12, 13, 13, 13, 11, 10, 10, 11, 11, 12, 12, 13, 13, 13, 13, 13, 13, 11, 10, 10, 11, 11, 11, 12, 12, 13, 13, 14, 13, 14, 11, 10, 11, 11, 12, 12, 12, 12, 13, 13, 14, 14, 14, 12, 11, 11, 12, 12, 12, 13, 13, 13, 14, 14, 14, 15, 12, 11, 12, 12, 12, 13, 13, 13, 13, 14, 14, 15, 15, 13, 12, 12, 12, 13, 13, 13, 13, 14, 14, 14, 14, 15],
    [6, 5, 6, 6, 7, 8, 9, 10, 10, 10, 11, 11, 12, 5, 4, 4, 5, 6, 7, 7, 8, 8, 9, 10, 10, 11, 6, 4, 5, 5, 6, 6, 7, 8, 8, 9, 9, 10, 10, 6, 5, 5, 5, 6, 7, 7, 8, 8, 9, 9, 10, 10, 7, 6, 6, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 8, 7, 6, 7, 7, 7, 8, 8, 8, 9, 10, 10, 11, 9, 7, 7, 7, 7, 8, 8, 9, 9, 9, 10, 10, 11, 9, 8, 8, 8, 8, 8, 9, 9, 9, 10, 10, 11, 11, 9, 8, 8, 8, 8, 8, 9, 9, 10, 10, 10, 11, 11, 10, 9, 9, 9, 9, 9, 9, 10, 10, 10, 11, 11, 12, 10, 9, 9, 9, 9, 10, 10, 10, 10, 11, 11, 11, 12, 11, 10, 9, 10, 10, 10, 10, 10, 11, 11, 11, 11, 12, 11, 10, 10, 10, 10, 10, 10, 11, 11, 12, 12, 12, 12],
    [4, 5, 6, 7, 8, 8, 9, 10, 10, 10, 11, 11, 12, 11, 12, 12, 10, 5, 4, 5, 6, 7, 7, 8, 8, 9, 9, 9, 10, 10, 10, 10, 11, 8, 6, 5, 5, 6, 7, 7, 8, 8, 8, 9, 9, 9, 10, 10, 10, 10, 8, 7, 6, 6, 6, 7, 7, 8, 8, 8, 9, 9, 9, 10, 10, 10, 10, 8, 8, 7, 7, 7, 7, 8, 8, 8, 8, 9, 9, 9, 10, 10, 10, 10, 8, 8, 7, 7, 7, 7, 8, 8, 8, 9, 9, 9, 9, 10, 10, 10, 10, 8, 9, 8, 8, 8, 8, 8, 8, 8, 9, 9, 9, 10, 10, 10, 10, 10, 8, 9, 8, 8, 8, 8, 8, 8, 9, 9, 9, 10, 10, 10, 10, 10, 10, 8, 10, 9, 8, 8, 9, 9, 9, 9, 9, 10, 10, 10, 10, 10, 10, 11, 8, 10, 9, 9, 9, 9, 9, 9, 9, 10, 10, 10, 10, 10, 10, 11, 11, 8, 11, 9, 9, 9, 9, 9, 9, 10, 10, 10, 10, 10, 11, 10, 11, 11, 8, 11, 10, 9, 9, 10, 9, 10, 10, 10, 10, 10, 11, 11, 11, 11, 11, 8, 11, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 11, 11, 11, 11, 11, 9, 11, 10, 9, 9, 10, 10, 10, 10, 10, 10, 11, 11, 11, 11, 11, 11, 9, 11, 10, 10, 10, 10, 10, 10, 10, 10, 10, 11, 11, 11, 11, 11, 11, 9, 12, 10, 10, 10, 10, 10, 10, 10, 11, 11, 11, 11, 11, 11, 12, 12, 9, 9, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 9, 5],
]
_SPEC_CODES = [
    [2040, 497, 2045, 1013, 104, 1008, 2039, 492, 2037, 1009, 114, 1012, 116, 17, 118, 491, 108, 1014, 2044, 481, 2033, 496, 97, 502, 2034, 490, 2043, 498, 105, 493, 119, 23, 111, 486, 100, 485, 103, 21, 98, 18, 0, 20, 101, 22, 109, 489, 99, 484, 107, 19, 113, 483, 112, 499, 2046, 487, 2035, 495, 96, 494, 2032, 482, 2042, 1011, 106, 488, 117, 16, 115, 500, 110, 1015, 2038, 480, 2041, 1010, 102, 501, 2047, 503, 2036],
    [499, 111, 509, 235, 35, 234, 503, 232, 506, 242, 45, 112, 32, 6, 43, 110, 40, 233, 505, 102, 248, 231, 27, 241, 500, 107, 501, 236, 42, 108, 44, 10, 39, 103, 26, 245, 36, 8, 31, 9, 0, 7, 29, 11, 48, 239, 28, 100, 30, 12, 41, 243, 47, 240, 508, 113, 498, 244, 33, 230, 247, 104, 504, 238, 34, 101, 49, 2, 38, 237, 37, 106, 507, 114, 510, 105, 46, 246, 511, 109, 502],
    [0, 9, 239, 11, 25, 240, 491, 486, 1010, 10, 53, 495, 52, 55, 489, 493, 487, 1011, 494, 1005, 8186, 492, 498, 2041, 2040, 1016, 4088, 8, 56, 1014, 54, 117, 1009, 1003, 1004, 4084, 24, 118, 2036, 57, 116, 1007, 499, 500, 2038, 488, 1002, 8188, 242, 497, 4091, 1013, 2035, 4092, 238, 1015, 32766, 496, 2037, 32765, 8187, 16378, 65535, 241, 1008, 16380, 490, 1006, 16379, 4086, 4090, 32764, 2034, 4085, 65534, 1012, 2039, 32763, 4087, 4089, 32762],
    [7, 22, 246, 24, 8, 239, 495, 243, 2040, 25, 23, 237, 21, 1, 226, 240, 112, 1008, 494, 241, 2042, 238, 228, 1010, 2038, 1007, 2045, 5, 20, 242, 9, 4, 229, 244, 232, 1012, 6, 2, 231, 3, 0, 107, 227, 105, 499, 235, 230, 1014, 110, 106, 500, 1004, 496, 1017, 245, 236, 2043, 234, 111, 1015, 2041, 1011, 4095, 233, 109, 1016, 108, 104, 501, 1006, 498, 2036, 2039, 1009, 4094, 1005, 497, 2037, 2046, 1013, 2044],
    [8191, 4087, 2036, 2024, 1009, 2030, 2041, 4088, 8189, 4093, 2033, 1000, 488, 240, 492, 1006, 2034, 4090, 4084, 1007, 498, 232, 112, 236, 496, 1002, 2035, 2027, 491, 234, 26, 8, 25, 238, 495, 2029, 1008, 242, 115, 11, 0, 10, 113, 243, 2025, 2031, 494, 239, 24, 9, 27, 235, 489, 2028, 2038, 1003, 499, 237, 114, 233, 497, 1005, 2039, 4086, 2032, 1001, 493, 241, 490, 1004, 2040, 4089, 8188, 4092, 4085, 2026, 1011, 1010, 2037, 4091, 8190],
    [2046, 1021, 497, 491, 500, 490, 496, 1020, 2045, 1014, 485, 234, 108, 113, 104, 240, 486, 1015, 499, 239, 50, 39, 40, 38, 49, 235, 503, 488, 111, 46, 8, 4, 6, 41, 107, 494, 495, 114, 45, 2, 0, 3, 47, 115, 506, 487, 110, 43, 7, 1, 5, 44, 109, 492, 505, 238, 48, 36, 42, 37, 51, 236, 498, 1016, 484, 237, 106, 112, 105, 116, 241, 1018, 2047, 1017, 502, 493, 504, 489, 501, 1019, 2044],
    [0, 5, 55, 116, 242, 491, 1005, 2039, 4, 12, 53, 113, 236, 238, 494, 501, 54, 52, 114, 234, 241, 489, 499, 1013, 115, 112, 235, 240, 497, 496, 1004, 1018, 243, 237, 488, 495, 1007, 1009, 1017, 2043, 493, 239, 490, 498, 1011, 1016, 2041, 2044, 1006, 492, 500, 1012, 1015, 2040, 4093, 4094, 2038, 1008, 1010, 1014, 2042, 2045, 4092, 4095],
    [14, 5, 16, 48, 111, 241, 506, 1022, 3, 0, 4, 18, 44, 106, 117, 248, 15, 2, 6, 20, 46, 105, 114, 245, 47, 17, 19, 42, 50, 108, 236, 250, 113, 43, 45, 49, 109, 112, 242, 505, 239, 104, 51, 107, 110, 238, 249, 1020, 504, 116, 115, 237, 240, 246, 502, 509, 1021, 243, 244, 247, 503, 507, 508, 1023],
    [0, 5, 55, 231, 478, 974, 985, 1992, 1997, 4040, 4061, 8164, 8172, 4, 12, 53, 114, 234, 237, 482, 977, 979, 992, 2008, 4047, 4053, 54, 52, 113, 232, 236, 481, 975, 989, 987, 2000, 4039, 4052, 4068, 230, 112, 233, 477, 483, 978, 988, 1996, 1994, 2014, 4056, 4074, 8155, 479, 235, 476, 486, 981, 990, 1995, 2013, 2012, 4045, 4066, 4071, 8161, 976, 480, 484, 982, 1989, 2001, 2011, 4050, 2016, 4057, 4075, 8163, 8169, 1988, 485, 983, 1990, 1999, 2010, 4043, 4058, 4067, 4073, 8166, 8179, 8183, 2003, 984, 993, 2004, 2009, 4051, 4062, 8157, 8153, 8162, 8170, 8177, 8182, 2002, 980, 986, 1991, 2007, 2018, 4046, 4059, 8152, 8174, 16368, 8180, 16370, 2017, 991, 1993, 2006, 4042, 4048, 4069, 4070, 8171, 8175, 16371, 16372, 16373, 4064, 1998, 2005, 4038, 4049, 4065, 8160, 8168, 8176, 16369, 16376, 16374, 32764, 4072, 2015, 4041, 4055, 4060, 8156, 8159, 8173, 8181, 16377, 16379, 32765, 32766, 8167, 4044, 4054, 4063, 8158, 8154, 8165, 8178, 16378, 16375, 16380, 16381, 32767],
    [34, 8, 29, 38, 95, 211, 463, 976, 983, 1005, 2032, 2038, 4093, 7, 0, 1, 9, 32, 84, 96, 213, 220, 468, 973, 990, 2023, 28, 2, 6, 12, 30, 40, 91, 205, 217, 462, 476, 985, 1009, 37, 11, 10, 13, 36, 87, 97, 204, 221, 460, 478, 979, 999, 93, 33, 31, 35, 39, 89, 100, 216, 223, 466, 482, 989, 1006, 209, 85, 41, 86, 88, 98, 206, 224, 226, 474, 980, 995, 2027, 457, 94, 90, 92, 99, 202, 218, 455, 458, 480, 987, 1000, 2028, 483, 210, 203, 208, 215, 219, 454, 469, 472, 970, 986, 2026, 2033, 481, 212, 207, 214, 222, 225, 464, 470, 977, 981, 1010, 2030, 2043, 1001, 461, 456, 459, 465, 471, 479, 975, 992, 1007, 2022, 2040, 4090, 1003, 477, 467, 473, 475, 978, 972, 988, 1002, 2029, 2035, 2041, 4089, 2034, 974, 484, 971, 984, 982, 994, 997, 2024, 2036, 2037, 2039, 4091, 2042, 1004, 991, 993, 996, 998, 1008, 2025, 2031, 4088, 4094, 4092, 4095],
    [0, 6, 25, 61, 156, 198, 423, 912, 962, 991, 2022, 2035, 4091, 2028, 4090, 4094, 910, 5, 1, 8, 20, 55, 66, 146, 175, 401, 421, 437, 926, 960, 930, 973, 2006, 174, 23, 7, 9, 24, 57, 64, 142, 163, 184, 409, 428, 449, 945, 918, 958, 970, 157, 60, 21, 22, 26, 59, 68, 145, 165, 190, 406, 430, 441, 929, 913, 933, 981, 148, 154, 54, 56, 58, 65, 140, 155, 176, 195, 414, 427, 444, 927, 911, 937, 975, 147, 191, 62, 63, 67, 69, 158, 167, 185, 404, 418, 442, 451, 934, 935, 955, 980, 159, 416, 143, 141, 144, 152, 166, 182, 196, 415, 431, 447, 921, 959, 948, 969, 999, 168, 438, 171, 164, 170, 178, 194, 197, 408, 420, 440, 908, 932, 964, 966, 989, 1000, 173, 943, 402, 189, 188, 398, 407, 410, 419, 433, 909, 920, 951, 979, 977, 987, 2013, 180, 990, 425, 411, 412, 417, 426, 429, 435, 907, 946, 952, 974, 993, 992, 2002, 2021, 183, 2019, 443, 424, 422, 432, 434, 439, 923, 922, 954, 949, 982, 2007, 996, 2008, 2026, 186, 2024, 928, 445, 436, 906, 452, 914, 938, 944, 956, 983, 2004, 2012, 2011, 2005, 2032, 193, 2043, 968, 931, 917, 925, 940, 942, 965, 984, 994, 998, 2020, 2023, 2016, 2025, 2039, 400, 2034, 915, 446, 448, 916, 919, 941, 963, 961, 978, 2010, 2009, 2015, 2027, 2036, 2042, 405, 2040, 957, 924, 939, 936, 947, 953, 976, 995, 997, 2018, 2014, 2029, 2033, 2041, 2044, 403, 4093, 988, 950, 967, 972, 971, 985, 986, 2003, 2017, 2030, 2031, 2037, 2038, 4092, 4095, 413, 450, 181, 161, 150, 151, 149, 153, 160, 162, 172, 169, 177, 179, 187, 192, 399, 4],
]
_QUADS = [(0,0,0,0), (0,0,0,1), (0,0,0,2), (0,0,1,0), (0,0,1,1), (0,0,1,2), (0,0,2,0), (0,0,2,1), (0,0,2,2), (0,1,0,0), (0,1,0,1), (0,1,0,2), (0,1,1,0), (0,1,1,1), (0,1,1,2), (0,1,2,0), (0,1,2,1), (0,1,2,2), (0,2,0,0), (0,2,0,1), (0,2,0,2), (0,2,1,0), (0,2,1,1), (0,2,1,2), (0,2,2,0), (0,2,2,1), (0,2,2,2), (1,0,0,0), (1,0,0,1), (1,0,0,2), (1,0,1,0), (1,0,1,1), (1,0,1,2), (1,0,2,0), (1,0,2,1), (1,0,2,2), (1,1,0,0), (1,1,0,1), (1,1,0,2), (1,1,1,0), (1,1,1,1), (1,1,1,2), (1,1,2,0), (1,1,2,1), (1,1,2,2), (1,2,0,0), (1,2,0,1), (1,2,0,2), (1,2,1,0), (1,2,1,1), (1,2,1,2), (1,2,2,0), (1,2,2,1), (1,2,2,2), (2,0,0,0), (2,0,0,1), (2,0,0,2), (2,0,1,0), (2,0,1,1), (2,0,1,2), (2,0,2,0), (2,0,2,1), (2,0,2,2), (2,1,0,0), (2,1,0,1), (2,1,0,2), (2,1,1,0), (2,1,1,1), (2,1,1,2), (2,1,2,0), (2,1,2,1), (2,1,2,2), (2,2,0,0), (2,2,0,1), (2,2,0,2), (2,2,1,0), (2,2,1,1), (2,2,1,2), (2,2,2,0), (2,2,2,1), (2,2,2,2)]
_SWB = {
    '96L': [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 64, 72, 80, 88, 96, 108, 120, 132, 144, 156, 172, 188, 212, 240, 276, 320, 384, 448, 512, 576, 640, 704, 768, 832, 896, 960, 1024],
    '64L': [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 64, 72, 80, 88, 100, 112, 124, 140, 156, 172, 192, 216, 240, 268, 304, 344, 384, 424, 464, 504, 544, 584, 624, 664, 704, 744, 784, 824, 864, 904, 944, 984, 1024],
    '64S': [0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 92, 128],
    '48L': [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 88, 96, 108, 120, 132, 144, 160, 176, 196, 216, 240, 264, 292, 320, 352, 384, 416, 448, 480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800, 832, 864, 896, 928, 1024],
    '48S': [0, 4, 8, 12, 16, 20, 28, 36, 44, 56, 68, 80, 96, 112, 128],
    '32L': [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 88, 96, 108, 120, 132, 144, 160, 176, 196, 216, 240, 264, 292, 320, 352, 384, 416, 448, 480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800, 832, 864, 896, 928, 960, 992, 1024],
    '24L': [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 52, 60, 68, 76, 84, 92, 100, 108, 116, 124, 136, 148, 160, 172, 188, 204, 220, 240, 260, 284, 308, 336, 364, 396, 432, 468, 508, 552, 600, 652, 704, 768, 832, 896, 960, 1024],
    '24S': [0, 4, 8, 12, 16, 20, 24, 28, 36, 44, 52, 64, 76, 92, 108, 128],
    '16L': [0, 8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 100, 112, 124, 136, 148, 160, 172, 184, 196, 212, 228, 244, 260, 280, 300, 320, 344, 368, 396, 424, 456, 492, 532, 572, 616, 664, 716, 772, 832, 896, 960, 1024],
    '16S': [0, 4, 8, 12, 16, 20, 24, 28, 32, 40, 48, 60, 72, 88, 108, 128],
    '8L': [0, 12, 24, 36, 48, 60, 72, 84, 96, 108, 120, 132, 144, 156, 172, 188, 204, 220, 236, 252, 268, 288, 308, 328, 348, 372, 396, 420, 448, 476, 508, 544, 580, 620, 664, 712, 764, 820, 880, 944, 1024],
    '8S': [0, 4, 8, 12, 16, 20, 24, 28, 36, 44, 52, 60, 72, 88, 108, 128],
}


# Spectrum codebook metadata: (dimension, is_unsigned, mod_value, max_len).
_CB_META = [
    (4, 0, 3, 11), (4, 0, 3, 9), (4, 1, 3, 16), (4, 1, 3, 12),
    (2, 0, 9, 13), (2, 0, 9, 11), (2, 1, 8, 12), (2, 1, 8, 10),
    (2, 1, 13, 15), (2, 1, 13, 12), (2, 1, 17, 12),
]
_SCF_MAX_LEN = 19

ZERO_HCB = 0
NOISE_HCB = 13
INTENSITY_HCB2 = 14
INTENSITY_HCB = 15
ESC_HCB = 11
EIGHT_SHORT = 2

ID_SCE, ID_CPE, ID_CCE, ID_LFE, ID_DSE, ID_PCE, ID_FIL, ID_END = range(8)

# Only the inserted-ad AAC media twin (u-<id>-a-96-<n>-<seg>.mp4); never its
# -i init segment, which carries no audio frames.
_AD_SEG = re.compile(r'u-\d+-a-96-\d+-\d+\.mp4')

# Candidate sample rates to probe (48 kHz covers 44.1 kHz too -- same band
# tables). The ad must parse cleanly under one of them or it is left untouched.
_RATES = (48000, 32000, 24000, 16000, 8000)

_STEP_DB = 1.5  # one global_gain step, informational only


class _PE(Exception):
    pass


def is_ad_segment(url: str) -> bool:
    """True for an inserted-ad AAC media segment URL (the attenuation target)."""
    return bool(url) and _AD_SEG.search(url) is not None


# ---------------------------------------------------------------------------
# Huffman lookup tables, built once from the ISO codebooks.
# Flat table: index = peek(max_len) bits, value = (symbol << 5) | code_len,
# 0 = no code. code_len <= 19 fits in 5 bits; symbol <= 288 fits above it.
# ---------------------------------------------------------------------------

def _build_table(lens, codes, max_len):
    table = [0] * (1 << max_len)
    for sym, (ln, code) in enumerate(zip(lens, codes)):
        if ln == 0:
            continue
        prefix = code << (max_len - ln)
        packed = (sym << 5) | ln
        for j in range(prefix, prefix + (1 << (max_len - ln))):
            table[j] = packed
    return table


_SCF_TABLE = _build_table(_SCF_LENS, _SCF_CODES, _SCF_MAX_LEN)
_SPEC_TABLES = [
    _build_table(_SPEC_LENS[i], _SPEC_CODES[i], _CB_META[i][3]) for i in range(11)
]


class _BR:
    """MSB-first bit reader over a bytes-like frame."""
    __slots__ = ('d', 'n', 'pos')

    def __init__(self, data):
        self.d = data
        self.n = len(data) * 8
        self.pos = 0

    def read_bit(self):
        p = self.pos
        if p >= self.n:
            raise _PE()
        self.pos = p + 1
        return (self.d[p >> 3] >> (7 - (p & 7))) & 1

    def read_bits(self, k):
        if k == 0:
            return 0
        p = self.pos
        if p + k > self.n:
            raise _PE()
        v = self._peek(p, k)
        self.pos = p + k
        return v

    def peek_bits(self, k):
        return self._peek(self.pos, k)

    def _peek(self, p, k):
        d = self.d
        byte = p >> 3
        bitoff = p & 7
        nbytes = (bitoff + k + 7) >> 3
        val = 0
        end = len(d)
        for i in range(nbytes):
            j = byte + i
            val = (val << 8) | (d[j] if j < end else 0)
        val >>= (nbytes * 8 - bitoff - k)
        return val & ((1 << k) - 1)

    def skip(self, k):
        if self.pos + k > self.n:
            raise _PE()
        self.pos += k

    def remaining(self):
        return self.n - self.pos


def _decode(br, table, max_len):
    e = table[br.peek_bits(max_len)]
    if e == 0:
        raise _PE()
    ln = e & 31
    if br.pos + ln > br.n:
        raise _PE()
    br.pos += ln
    return e >> 5


def _swb(rate):
    if rate >= 75132:
        return _SWB['96L'], _SWB['64S']
    if rate >= 55426:
        return _SWB['64L'], _SWB['64S']
    if rate >= 37566:
        return _SWB['48L'], _SWB['48S']
    if rate >= 27713:
        return _SWB['32L'], _SWB['48S']
    if rate >= 18783:
        return _SWB['24L'], _SWB['24S']
    if rate >= 9391:
        return _SWB['16L'], _SWB['16S']
    return _SWB['8L'], _SWB['8S']


# ---------------------------------------------------------------------------
# AAC-LC bitstream syntax (ISO/IEC 14496-3, 4.4-4.5).
# ---------------------------------------------------------------------------

def _skip_ltp(br, max_sfb):
    # ltp_data() for a long window (AAC-LC long-term prediction): ltp_lag (11) +
    # ltp_coef (3) + ltp_long_used[min(max_sfb, MAX_LTP_LONG_SFB=40)] (1 bit each).
    br.skip(14)
    br.skip(max_sfb if max_sfb < 40 else 40)


def _parse_ics_info(br):
    br.read_bits(1)                     # ics_reserved_bit
    window_sequence = br.read_bits(2)
    br.read_bits(1)                     # window_shape
    long_win = window_sequence != EIGHT_SHORT
    if long_win:
        max_sfb = br.read_bits(6)
        pred = br.read_bit()            # predictor_data_present
        if pred:
            # AAC-LC signals LTP here (not AAC-MAIN prediction): skip this channel's
            # ltp_data when present. DirecTV's HE-AAC uses LTP, so this must be parsed,
            # not rejected.
            if br.read_bit():           # ltp_data_present
                _skip_ltp(br, max_sfb)
        return max_sfb, True, 1, [1], pred
    max_sfb = br.read_bits(4)
    grouping = br.read_bits(7)
    groups = 1
    group_len = [1]
    for i in range(7):
        if (grouping >> (6 - i)) & 1 == 0:
            groups += 1
            group_len.append(1)
        else:
            group_len[groups - 1] += 1
    return max_sfb, False, groups, group_len, 0


def _parse_section_data(br, max_sfb, long_win, groups):
    sect_bits = 5 if long_win else 3
    esc = (1 << sect_bits) - 1
    out = []
    for _ in range(groups):
        cbs = [0] * max_sfb
        k = 0
        while k < max_sfb:
            cb = br.read_bits(4)
            if cb == 12:
                raise _PE()
            length = 0
            while True:
                incr = br.read_bits(sect_bits)
                length += incr
                if incr < esc:
                    break
            if length == 0 or k + length > max_sfb:
                raise _PE()
            for s in range(k, k + length):
                cbs[s] = cb
            k += length
        out.append(cbs)
    return out


def _parse_scale_factors(br, max_sfb, groups, sfb_cb):
    noise_pcm = True
    for g in range(groups):
        for sfb in range(max_sfb):
            cb = sfb_cb[g][sfb]
            if cb == ZERO_HCB:
                continue
            if cb == NOISE_HCB and noise_pcm:
                br.read_bits(9)
                noise_pcm = False
                continue
            _decode(br, _SCF_TABLE, _SCF_MAX_LEN)


def _read_escape(br):
    n = 0
    while br.read_bit():
        n += 1
        if n >= 9:
            raise _PE()
    br.skip(n + 4)


def _parse_spectral(br, max_sfb, groups, group_len, sfb_cb, bands):
    for g in range(groups):
        for sfb in range(max_sfb):
            cb = sfb_cb[g][sfb]
            if cb in (ZERO_HCB, NOISE_HCB, INTENSITY_HCB, INTENSITY_HCB2):
                continue
            width = bands[sfb + 1] - bands[sfb]
            dim, unsigned, mod, mx = _CB_META[cb - 1]
            table = _SPEC_TABLES[cb - 1]
            ncw = width // dim
            for _w in range(group_len[g]):
                for _ in range(ncw):
                    sym = _decode(br, table, mx)
                    if not unsigned:
                        continue
                    if dim == 4:
                        for v in _QUADS[sym]:
                            if v != 0:
                                br.read_bit()
                    else:
                        x = sym // mod
                        y = sym % mod
                        if x != 0:
                            br.read_bit()
                        if y != 0:
                            br.read_bit()
                        if cb == ESC_HCB:
                            if x == 16:
                                _read_escape(br)
                            if y == 16:
                                _read_escape(br)


def _parse_pulse(br):
    n = br.read_bits(2)
    br.read_bits(6)
    for _ in range(n + 1):
        br.read_bits(5)
        br.read_bits(4)


def _parse_tns(br, max_sfb, long_win):
    nfb = 2 if long_win else 1
    lenb = 6 if long_win else 4
    ordb = 5 if long_win else 3
    nwin = 1 if long_win else 8
    for _ in range(nwin):
        n_filt = br.read_bits(nfb)
        if n_filt <= 0:
            continue
        coef_res = br.read_bits(1)
        for _ in range(n_filt):
            br.read_bits(lenb)                       # filter SFB length (value unused for skipping)
            order = br.read_bits(ordb)
            if order > 0:
                br.read_bits(1)                      # direction
                coef_compress = br.read_bits(1)
                br.skip(order * (coef_res + 3 - coef_compress))


def _parse_ics(br, shared, bands_long, bands_short, gains):
    gains.append([br.pos, br.peek_bits(8)])  # global_gain location + value
    br.skip(8)
    if shared is None:
        info = _parse_ics_info(br)
    else:
        info = shared
    max_sfb, long_win, groups, group_len, _pred = info
    bands = bands_long if long_win else bands_short
    if max_sfb >= len(bands):
        raise _PE()
    sfb_cb = _parse_section_data(br, max_sfb, long_win, groups)
    _parse_scale_factors(br, max_sfb, groups, sfb_cb)
    if br.read_bit():                        # pulse_data_present
        if not long_win:
            raise _PE()
        _parse_pulse(br)
    if br.read_bit():                        # tns_data_present
        _parse_tns(br, max_sfb, long_win)
    if br.read_bit():                        # gain_control_data_present
        raise _PE()
    _parse_spectral(br, max_sfb, groups, group_len, sfb_cb, bands)
    return info


def _parse_sce(br, bl, bs, gains):
    br.read_bits(4)                          # element_instance_tag
    _parse_ics(br, None, bl, bs, gains)


def _parse_cpe(br, bl, bs, gains):
    br.read_bits(4)
    common = br.read_bit()
    shared = None
    if common:
        shared = _parse_ics_info(br)
        if br.read_bits(2) == 1:             # ms_mask_present == 1
            br.skip(shared[2] * shared[0])   # groups * max_sfb ms_used bits
        if shared[4] and shared[1]:          # predictor present + long: 2nd channel's LTP
            if br.read_bit():                # ltp_data_present (channel 1)
                _skip_ltp(br, shared[0])
    _parse_ics(br, shared, bl, bs, gains)
    _parse_ics(br, shared, bl, bs, gains)


def _skip_dse(br):
    br.read_bits(4)
    align = br.read_bit()
    count = br.read_bits(8)
    if count == 255:
        count += br.read_bits(8)
    if align and (br.pos & 7):
        br.pos = (br.pos + 7) & ~7
    br.skip(count * 8)


def _skip_fil(br):
    count = br.read_bits(4)
    if count == 15:
        count += br.read_bits(8) - 1
        if count < 0:
            count = 0
    br.skip(count * 8)


def _skip_pce(br):
    raise _PE()  # PCE is unexpected in a media fragment; bail -> leave frame untouched


def _parse_frame(frame, bl, bs):
    """Return (gain_locations, saw_end). Raises _PE on any malformed element."""
    br = _BR(frame)
    gains = []
    saw_end = False
    while True:
        if br.remaining() < 3:
            break
        el = br.read_bits(3)
        if el in (ID_SCE, ID_LFE):
            _parse_sce(br, bl, bs, gains)
        elif el == ID_CPE:
            _parse_cpe(br, bl, bs, gains)
        elif el == ID_DSE:
            _skip_dse(br)
        elif el == ID_PCE:
            _skip_pce(br)
        elif el == ID_FIL:
            _skip_fil(br)
        elif el == ID_END:
            saw_end = True
            break
        else:                                # ID_CCE or unknown
            raise _PE()
    # A well-formed sample is one byte-aligned raw_data_block: after END only
    # sub-byte padding is left. Anything else means this rate desynced.
    return gains, saw_end, br.remaining()


# ---------------------------------------------------------------------------
# Fragmented-MP4 container (ISO/IEC 14496-12): moof -> traf -> trun sizes.
# ---------------------------------------------------------------------------

def _boxes(buf, start, end):
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i:i + 4], 'big')
        typ = buf[i + 4:i + 8]
        hdr = 8
        if size == 1:
            size = int.from_bytes(buf[i + 8:i + 16], 'big')
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            return
        yield typ, i + hdr, i + size
        i += size


def _find(buf, start, end, want):
    for typ, ds, de in _boxes(buf, start, end):
        if typ == want:
            return ds, de
    return None


def _trun_sizes(buf, ds, de):
    flags = int.from_bytes(buf[ds + 1:ds + 4], 'big')
    count = int.from_bytes(buf[ds + 4:ds + 8], 'big')
    off = ds + 8
    if flags & 0x1:
        off += 4
    if flags & 0x4:
        off += 4
    sizes = []
    for _ in range(count):
        if flags & 0x100:
            off += 4
        if flags & 0x200:
            sizes.append(int.from_bytes(buf[off:off + 4], 'big'))
            off += 4
        else:
            sizes.append(None)
        if flags & 0x400:
            off += 4
        if flags & 0x800:
            off += 4
    if off > de:
        raise _PE()
    return sizes


def _tfhd_default_size(buf, ds):
    flags = int.from_bytes(buf[ds + 1:ds + 4], 'big')
    p = ds + 4 + 4                           # version/flags + track_ID
    if flags & 0x1:
        p += 8
    if flags & 0x2:
        p += 4
    if flags & 0x8:
        p += 4
    if flags & 0x10:
        return int.from_bytes(buf[p:p + 4], 'big')
    return None


def _write_u8(buf, bitpos, val):
    for i in range(8):
        p = bitpos + i
        bi = p >> 3
        mask = 1 << (7 - (p & 7))
        if (val >> (7 - i)) & 1:
            buf[bi] |= mask
        else:
            buf[bi] &= ~mask


def attenuate_ad_segment(data: bytes, steps: int = 3) -> bytes:
    """Return ``data`` with every AAC-LC global_gain lowered by ``steps`` (1.5 dB
    each), or the original bytes unchanged if it is not a cleanly-parseable AAC
    fMP4 media fragment. Never raises; never returns a corrupted segment."""
    try:
        if steps <= 0 or not data:
            return data
        buf = bytearray(data)
        moof = _find(buf, 0, len(buf), b'moof')
        mdat = _find(buf, 0, len(buf), b'mdat')
        if not moof or not mdat:
            return data                      # init segment or not fragmented MP4
        mdat_ds, mdat_de = mdat
        sizes = []
        for typ, ds, de in _boxes(buf, moof[0], moof[1]):
            if typ != b'traf':
                continue
            default = None
            tf = _find(buf, ds, de, b'tfhd')
            if tf:
                default = _tfhd_default_size(buf, tf[0])
            for t2, ds2, de2 in _boxes(buf, ds, de):
                if t2 == b'trun':
                    for sz in _trun_sizes(buf, ds2, de2):
                        sizes.append(sz if sz is not None else default)
        if not sizes or any(s is None for s in sizes):
            return data
        if sum(sizes) != mdat_de - mdat_ds:
            return data                      # unexpected mdat layout

        frames = []
        cum = mdat_ds
        for sz in sizes:
            frames.append((cum, sz))
            cum += sz

        for rate in _RATES:
            bl, bs = _swb(rate)
            collected = []
            ok = True
            for start, sz in frames:
                try:
                    gains, saw_end, rem = _parse_frame(data[start:start + sz], bl, bs)
                except Exception:
                    ok = False
                    break
                if not saw_end or not gains or rem >= 8:
                    ok = False
                    break
                collected.append((start, gains))
            if ok:
                break
        else:
            return data                      # no sample rate parsed every frame

        for start, gains in collected:
            for bitpos, old in gains:
                new = old - steps
                if new < 0:
                    new = 0
                _write_u8(buf, start * 8 + bitpos, new)
        return bytes(buf)
    except Exception:
        return data
