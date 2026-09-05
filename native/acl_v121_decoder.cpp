// Decode the ACL 1.2 compressed clip embedded in Onmyoji RAWANIMA v0 files.
// ACL is MIT licensed: https://github.com/nfrechette/acl/tree/v1.2.1

#include "acl/core/compressed_clip.h"
#include "acl/core/interpolation_utils.h"
#include "acl/core/utils.h"
#include "acl/algorithm/uniformly_sampled/decoder.h"
#include "acl/decompression/default_output_writer.h"
#include "acl/math/quat_32.h"
#include "acl/math/vector4_32.h"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

namespace {

template <typename T>
bool write_value(std::ofstream& output, const T& value) {
    output.write(reinterpret_cast<const char*>(&value), sizeof(T));
    return output.good();
}

uint32_t read_u32(const uint8_t* ptr) {
    uint32_t value = 0;
    std::memcpy(&value, ptr, sizeof(value));
    return value;
}

size_t find_tag(const std::vector<uint8_t>& data, const char tag[4]) {
    for (size_t index = 0; index + 8 <= data.size(); ++index) {
        if (std::memcmp(data.data() + index, tag, 4) == 0)
            return index;
    }
    return std::string::npos;
}

} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc != 3) {
        std::wcerr << L"usage: onmyoji_acl_decode.exe input.rawanimation output.nanim\n";
        return 2;
    }

    std::ifstream input(argv[1], std::ios::binary);
    if (!input) {
        std::wcerr << L"cannot open input\n";
        return 3;
    }
    input.seekg(0, std::ios::end);
    const std::streamoff input_size = input.tellg();
    input.seekg(0, std::ios::beg);
    if (input_size < 32) {
        std::wcerr << L"input is too small\n";
        return 4;
    }
    std::vector<uint8_t> data(static_cast<size_t>(input_size));
    input.read(reinterpret_cast<char*>(data.data()), input_size);
    if (!input || std::memcmp(data.data(), "RAWANIMA", 8) != 0) {
        std::wcerr << L"not a RAWANIMA file\n";
        return 5;
    }
    if (read_u32(data.data() + 16) != 0) {
        std::wcerr << L"only RAWANIMA v0 is supported\n";
        return 6;
    }

    const size_t data_tag = find_tag(data, "DATA");
    if (data_tag == std::string::npos || data_tag + 24 > data.size()) {
        std::wcerr << L"DATA section not found\n";
        return 7;
    }
    const uint32_t section_size = read_u32(data.data() + data_tag + 4);
    const size_t clip_offset = data_tag + 8;
    const uint32_t clip_size = read_u32(data.data() + clip_offset);
    if (clip_size < sizeof(acl::CompressedClip) || clip_size > section_size ||
        clip_offset + clip_size > data.size()) {
        std::wcerr << L"invalid ACL clip size\n";
        return 8;
    }

    // CompressedClip explicitly requires 16-byte alignment.
    void* aligned = _aligned_malloc(clip_size, alignof(acl::CompressedClip));
    if (aligned == nullptr) {
        std::wcerr << L"out of memory\n";
        return 9;
    }
    std::unique_ptr<void, decltype(&_aligned_free)> clip_memory(aligned, &_aligned_free);
    std::memcpy(aligned, data.data() + clip_offset, clip_size);
    const auto& clip = *reinterpret_cast<const acl::CompressedClip*>(aligned);
    const acl::ErrorResult validation = clip.is_valid(true);
    if (validation.any()) {
        std::cerr << "invalid ACL clip: " << validation.c_str() << "\n";
        return 10;
    }
    if (clip.get_algorithm_type() != acl::AlgorithmType8::UniformlySampled) {
        std::wcerr << L"unsupported ACL algorithm\n";
        return 11;
    }

    const acl::ClipHeader& header = acl::get_clip_header(clip);
    const uint16_t bone_count = header.num_bones;
    const uint32_t sample_count = header.num_samples;
    const float sample_rate = header.sample_rate;
    const float duration = acl::calculate_duration(sample_count, sample_rate);
    if (bone_count == 0 || sample_count == 0 || !(sample_rate > 0.0f)) {
        std::wcerr << L"empty ACL clip\n";
        return 12;
    }

    std::ofstream output(argv[2], std::ios::binary | std::ios::trunc);
    if (!output) {
        std::wcerr << L"cannot open output\n";
        return 13;
    }
    output.write("NANIM001", 8);
    const uint16_t flags = header.has_scale ? 1 : 0;
    write_value(output, bone_count);
    write_value(output, flags);
    write_value(output, sample_count);
    write_value(output, sample_rate);
    write_value(output, duration);

    using Context = acl::uniformly_sampled::DecompressionContext<
        acl::uniformly_sampled::DefaultDecompressionSettings>;
    Context context;
    context.initialize(clip);
    std::vector<acl::Transform_32> transforms(bone_count);
    acl::DefaultOutputWriter writer(transforms.data(), bone_count);
    for (uint32_t sample_index = 0; sample_index < sample_count; ++sample_index) {
        const float sample_time = std::min(float(sample_index) / sample_rate, duration);
        context.seek(sample_time, acl::SampleRoundingPolicy::Nearest);
        context.decompress_pose(writer);
        for (const acl::Transform_32& transform : transforms) {
            const float values[10] = {
                acl::vector_get_x(transform.translation),
                acl::vector_get_y(transform.translation),
                acl::vector_get_z(transform.translation),
                acl::quat_get_x(transform.rotation),
                acl::quat_get_y(transform.rotation),
                acl::quat_get_z(transform.rotation),
                acl::quat_get_w(transform.rotation),
                acl::vector_get_x(transform.scale),
                acl::vector_get_y(transform.scale),
                acl::vector_get_z(transform.scale),
            };
            output.write(reinterpret_cast<const char*>(values), sizeof(values));
        }
    }
    if (!output.good()) {
        std::wcerr << L"failed while writing output\n";
        return 14;
    }
    return 0;
}
