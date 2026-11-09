idf_component_register(SRCS "main.c" INCLUDE_DIRS ".")

# Optional overrides for main.c settings, e.g. CSI_DEFS="CSI_PROBE=1 BUZZER_GPIO=4"
if(DEFINED ENV{CSI_DEFS})
    separate_arguments(csi_defs UNIX_COMMAND "$ENV{CSI_DEFS}")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ${csi_defs})
endif()
