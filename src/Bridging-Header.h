#pragma once

#import <CoreFoundation/CoreFoundation.h>
#import <IOKit/IOKitLib.h>

typedef CFTypeRef IOAVService;

extern IOAVService IOAVServiceCreateWithService(CFAllocatorRef allocator, io_service_t service);
extern IOReturn IOAVServiceReadI2C(
    IOAVService service,
    uint32_t chipAddress,
    uint32_t offset,
    void *outputBuffer,
    uint32_t outputBufferSize
);
extern IOReturn IOAVServiceWriteI2C(
    IOAVService service,
    uint32_t chipAddress,
    uint32_t dataAddress,
    void *inputBuffer,
    uint32_t inputBufferSize
);

extern int DisplayServicesGetBrightness(uint32_t display, float *brightness);
extern int DisplayServicesSetBrightness(uint32_t display, float brightness);
